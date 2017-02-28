# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import cgi
from collections import namedtuple
import hashlib
import io
import logging
import time
import zlib

from everett.component import ConfigOptions, RequiredConfigMixin
from everett.manager import parse_class
import falcon
import gevent
from gevent.pool import Pool

from antenna import metrics
from antenna.throttler import (
    ACCEPT,
    DEFER,
    REJECT,
    RESULT_TO_TEXT,
    Throttler,
)
from antenna.util import (
    create_crash_id,
    de_null,
    utc_now,
)


logger = logging.getLogger(__name__)
mymetrics = metrics.get_metrics(__name__)


CrashReport = namedtuple('CrashReport', ['raw_crash', 'dumps', 'crash_id'])


class SaveQueue:
    """Simple FIFO queue that's not thread-safe

    This is mostly to get a cheap convenient API.

    """
    def __init__(self):
        self._queue = []

    def __len__(self):
        return len(self._queue)

    def add(self, item):
        """Adds an item to the end of the queue"""
        self._queue.append(item)

    def next(self):
        """Returns the next item or None"""
        if self._queue:
            return self._queue.pop(0)
        return None


def positive_int(val):
    """Everett parser that enforces val >= 1"""
    val = int(val)
    if val < 1:
        raise ValueError('val must be greater than 1: %s' % val)
    return val


class BreakpadSubmitterResource(RequiredConfigMixin):
    """Handles incoming breakpad crash reports and saves to crashstorage

    This handles incoming HTTP POST requests containing breakpad-style crash
    reports in multipart/form-data format.

    It can handle compressed or uncompressed POST payloads.

    It parses the payload from the HTTP POST request, runs it through the
    throttler with the specified rules, generates a crash_id, returns the
    crash_id to the HTTP client and then saves the crash using the configured
    crashstorage class.

    .. Note::

       From when a crash comes in to when it's saved by the crashstorage class,
       the crash is entirely in memory. Keep that in mind when figuring out
       how to scale your Antenna nodes.


    The most important configuration bit here is choosing the crashstorage
    class.

    For example::

        CRASHSTORAGE_CLASS=antenna.ext.s3.crashstorage.S3CrashStorage

    """
    required_config = ConfigOptions()
    required_config.add_option(
        'dump_field', default='upload_file_minidump',
        doc='the name of the field in the POST data for dumps'
    )
    required_config.add_option(
        'dump_id_prefix', default='bp-',
        doc='the crash type prefix'
    )
    required_config.add_option(
        'crashstorage_class',
        default='antenna.ext.crashstorage_base.NoOpCrashStorage',
        parser=parse_class,
        doc='the class in charge of storing crashes'
    )
    required_config.add_option(
        'concurrent_saves',
        default='10',
        parser=positive_int,
        doc='max number of crash reports being saved concurrently; minimum of 1'
    )

    def __init__(self, config):
        self.config = config.with_options(self)
        self.crashstorage = self.config('crashstorage_class')(config.with_namespace('crashstorage'))
        self.throttler = Throttler(config)

        # Gevent pool for handling incoming crash reports
        self.pipeline_pool = Pool(size=self.config('concurrent_saves'))

        self.save_queue = SaveQueue()
        self.mymetrics = metrics.get_metrics(self)

        # Kick off the heartbeat
        gevent.spawn(self.heartbeat)

    def get_runtime_config(self, namespace=None):
        for item in super().get_runtime_config():
            yield item

        for item in self.throttler.get_runtime_config():
            yield item

        for item in self.crashstorage.get_runtime_config(['crashstorage']):
            yield item

    def check_health(self, state):
        if hasattr(self.crashstorage, 'check_health'):
            self.crashstorage.check_health(state)

    def heartbeat(self):
        """Heartbeat function

        Every 30 seconds, runs a set of methods on this class.

        """
        while True:
            gevent.sleep(30)
            try:
                self.health_stats()
            except Exception:
                logger.exception('Exception thrown while retrieving health stats')

    def health_stats(self):
        # The number of crash reports sitting in the queue
        self.mymetrics.gauge('save_queue_size', len(self.save_queue))

        # The number of actively running coroutines saving crashes
        self.mymetrics.gauge('active_save_workers', len(self.pipeline_pool))

    def extract_payload(self, req):
        """Parses the HTTP POST payload

        Decompresses the payload if necessary and then walks through the
        FieldStorage converting from multipart/form-data to Python datatypes.

        NOTE(willkg): The FieldStorage is poorly documented (in my opinion). It
        has a list attribute that is a list of FieldStorage items--one for each
        key/val in the form. For attached files, the FieldStorage will have a
        name, value and filename and the type should be
        application/octet-stream. Thus we parse it looking for things of type
        text/plain and application/octet-stream.

        :arg req: a Falcon Request instance

        :returns: (raw_crash dict, dumps dict)

        """
        # If we don't have a content type, return an empty crash
        if not req.content_type:
            return {}, {}

        # If it's the wrong content type or there's no boundary section, return
        # an empty crash
        content_type = [part.strip() for part in req.content_type.split(';', 1)]
        if ((len(content_type) != 2 or
             content_type[0] != 'multipart/form-data' or
             not content_type[1].startswith('boundary='))):
            return {}, {}

        content_length = req.content_length or 0

        # If there's no content, return an empty crash
        if content_length == 0:
            return {}, {}

        # Decompress payload if it's compressed
        if req.env.get('HTTP_CONTENT_ENCODING') == 'gzip':
            self.mymetrics.incr('gzipped_crash')

            # If the content is gzipped, we pull it out and decompress it. We
            # have to do that here because nginx doesn't have a good way to do
            # that in nginx-land.
            gzip_header = 16 + zlib.MAX_WBITS
            try:
                data = zlib.decompress(req.stream.read(content_length), gzip_header)
            except zlib.error:
                # This indicates this isn't a valid compressed stream. Given
                # that the HTTP request insists it is, we're just going to
                # assume it's junk and not try to process any further.
                self.mymetrics.incr('bad_gzipped_crash')
                return {}, {}

            # Stomp on the content length to correct it because we've changed
            # the payload size by decompressing it. We save the original value
            # in case we need to debug something later on.
            req.env['ORIG_CONTENT_LENGTH'] = content_length
            req.env['CONTENT_LENGTH'] = str(len(data))
            content_length = len(data)

            data = io.BytesIO(data)
            self.mymetrics.histogram('crash_size.compressed', content_length)
        else:
            data = io.BytesIO(req.stream.read(req.content_length or 0))
            self.mymetrics.histogram('crash_size.uncompressed', content_length)

        fs = cgi.FieldStorage(fp=data, environ=req.env, keep_blank_values=1)

        # NOTE(willkg): In the original collector, this returned request
        # querystring data as well as request body data, but we're not doing
        # that because the query string just duplicates data in the payload.

        raw_crash = {}
        dumps = {}

        for fs_item in fs.list:
            if fs_item.name == 'dump_checksums':
                # We don't want to pick up the dump_checksums from a raw
                # crash that was re-submitted.
                continue

            elif fs_item.type and (fs_item.type.startswith('application/octet-stream') or isinstance(fs_item.value, bytes)):
                # This is a dump, so we get a checksum and save the bits in the
                # relevant places.

                # FIXME(willkg): The dump name is essentially user-provided. We should
                # sanitize it before proceeding.
                dumps[fs_item.name] = fs_item.value
                checksum = hashlib.md5(fs_item.value).hexdigest()
                raw_crash.setdefault('dump_checksums', {})[fs_item.name] = checksum

            else:
                # This isn't a dump, so it's a key/val pair, so we add that.
                raw_crash[fs_item.name] = de_null(fs_item.value)

        return raw_crash, dumps

    def get_throttle_result(self, raw_crash):
        """Given a raw_crash, figures out the throttling

        If the raw_crash contains throttling information already, it returns
        that. If it doesn't, then this will apply throttling and return the
        results of that.

        A rule name of ``ALREADY_THROTTLED`` indicates that the raw_crash was
        previously throttled and we're re-using that data.

        A rule name of ``THROTTLEABLE_0`` indicates that the raw_crash was
        marked to not be throttled.

        :arg dict raw_crash: the raw crash to throttle

        :returns tuple: ``(result, rule_name, percentage)``

        """
        # If we have throttle results for this crash, return those.
        if 'legacy_processing' in raw_crash and 'throttle_rate' in raw_crash:
            try:
                result = int(raw_crash['legacy_processing'])
                if result not in (ACCEPT, DEFER):
                    raise ValueError('Result is not a valid value: %r', result)

                throttle_rate = int(raw_crash['throttle_rate'])
                if not (0 <= throttle_rate <= 100):
                    raise ValueError('Throttle rate is not a valid value: %r', result)
                return result, 'ALREADY_THROTTLED', throttle_rate

            except ValueError:
                # If we've gotten a ValueError, it means one or both of the
                # values is bad and we should ignore it and move forward.
                self.mymetrics.incr('throttle.bad_throttle_values')

        if raw_crash.get('Throttleable', None) == '0':
            # If the raw crash has ``Throttleable=0``, then we accept the
            # crash.
            self.mymetrics.incr('throttleable_0')
            result = ACCEPT
            rule_name = 'THROTTLEABLE_0'
            throttle_rate = 100

        else:
            # At this stage, nothing has given us a throttle answer, so we
            # throttle the crash.
            result, rule_name, throttle_rate = self.throttler.throttle(raw_crash)

        # Save the results in the raw_crash itself
        raw_crash['legacy_processing'] = result
        raw_crash['throttle_rate'] = throttle_rate

        return result, rule_name, throttle_rate

    @mymetrics.timer_decorator('BreakpadSubmitterResource.on_post.time')
    def on_post(self, req, resp):
        start_time = time.time()
        resp.content_type = 'text/plain'

        raw_crash, dumps = self.extract_payload(req)

        self.mymetrics.incr('incoming_crash')

        current_timestamp = utc_now()
        raw_crash['submitted_timestamp'] = current_timestamp.isoformat()
        raw_crash['timestamp'] = start_time

        # We throttle first because throttling affects generation of new crash
        # ids and we want to do all our logging with the correct crash id to
        # make it easier to follow crashes through Antenna.
        result, rule_name, percentage = self.get_throttle_result(raw_crash)

        if 'uuid' in raw_crash:
            # FIXME(willkg): This means the uuid is essentially user-provided.
            # We should sanitize it before proceeding.
            crash_id = raw_crash['uuid']
            logger.info('%s has existing crash_id', crash_id)

        else:
            crash_id = create_crash_id(timestamp=current_timestamp, throttle_result=result)
            raw_crash['uuid'] = crash_id

        raw_crash['type_tag'] = self.config('dump_id_prefix').strip('-')

        # Log the throttle result
        logger.info('%s: matched by %s; returned %s', crash_id, rule_name, RESULT_TO_TEXT[result])

        if raw_crash['legacy_processing'] is ACCEPT:
            self.mymetrics.incr('throttle.accept')

        elif raw_crash['legacy_processing'] is DEFER:
            self.mymetrics.incr('throttle.defer')

        elif raw_crash['legacy_processing'] is REJECT:
            # Reject the crash and end processing.
            self.mymetrics.incr('throttle.reject')

            resp.status = falcon.HTTP_200
            resp.body = 'Discarded=1'
            return

        self.add_to_queue(CrashReport(raw_crash, dumps, crash_id))

        resp.status = falcon.HTTP_200
        resp.body = 'CrashID=%s%s\n' % (self.config('dump_id_prefix'), crash_id)

    def add_to_queue(self, crash_report):
        """Adds a crash report to the save queue

        As a side-effect, this also spins off a coroutine to deal with the
        crash report.

        """
        self.save_queue.add(crash_report)

        # Check the pool and spawn a new co-routine if possible
        if self.pipeline_pool.free_count() > 0:
            self.pipeline_pool.spawn(self.process_queue)

    def process_queue(self):
        """Processes the save queue until it's empty"""
        # Process until the queue is empty
        while len(self.save_queue) > 0:
            crash_report = self.save_queue.next()
            try:
                self.save_crash_to_storage(crash_report)
            except Exception:
                self.add_to_queue(crash_report)

    def save_crash_to_storage(self, crash_report):
        """Saves a crash to storage

        If this raises an error, then that bubbles up and the caller can retry
        later.

        """
        crash_id = crash_report.crash_id
        dumps = crash_report.dumps
        raw_crash = crash_report.raw_crash

        # Save dumps to crashstorage
        self.crashstorage.save_dumps(
            crash_id,
            dumps
        )

        # Save the raw crash metadata to crashstorage
        self.crashstorage.save_raw_crash(
            crash_id,
            raw_crash
        )

        # Capture the total time it took for this crash to be handled from post
        # to s3 save and log the crash id
        #
        # NOTE(willkg): time.time returns seconds, but .timing() wants
        # milliseconds, so we multiply!
        delta = (time.time() - raw_crash['timestamp']) * 1000
        self.mymetrics.timing('crash_handling.time', delta)

        self.mymetrics.incr('save_crash.count')
        logger.info('%s saved', crash_id)

    def join_pool(self):
        """Joins the pool--use only in tests!

        This is helpful for forcing all the coroutines to complete so that we
        can verify outcomes in the test suite for work that might cross
        coroutines.

        """
        self.pipeline_pool.join()
