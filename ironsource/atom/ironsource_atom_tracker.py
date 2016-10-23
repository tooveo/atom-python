import json
import signal

from ironsource.atom.ironsource_atom import IronSourceAtom
from ironsource.atom.queue_event_storage import QueueEventStorage
from ironsource.atom.batch_event_pool import BatchEventPool
from ironsource.atom.event import Event
import ironsource.atom.atom_logger as logger
import ironsource.atom.config as config

import time
import random

from threading import Lock
from threading import Thread


class IronSourceAtomTracker:
    """
       ironSource Atom high level API class (Tracker), supports: track() and flush()
    """
    TAG = "IronSourceAtomTracker"

    # todo: add flush_interval to readme
    def __init__(self,
                 batch_worker_count=config.BATCH_WORKER_COUNT,
                 batch_pool_size=config.BATCH_POOL_SIZE,
                 backlog_size=config.BACKLOG_SIZE,
                 flush_interval=config.FLUSH_INTERVAL,
                 retry_max_time=config.RETRY_MAX_TIME,
                 retry_max_count=config.RETRY_MAX_COUNT,
                 batch_size=config.BATCH_SIZE,
                 batch_bytes_size=config.BATCH_BYTES_SIZE,
                 is_debug=False,
                 callback=None):
        """
        :param batch_worker_count:
        :param batch_pool_size:
        :param backlog_size:
        :param flush_interval:
        :param callback:
        """

        self._atom = IronSourceAtom()
        self._is_debug = is_debug

        # Optional callback to be called on error, convention: time, status, msg
        self._callback = callback if callable(callback) else lambda timestamp, status, error_msg, data: None

        self._logger = logger.get_logger(debug=self._is_debug)

        self._is_run_worker = True
        self._is_flush_data = False
        # Flush all methods
        self._is_flush_all = False

        # calculate current milliseconds
        self._current_milliseconds = lambda: int(round(time.time() * 1000))

        self._data_lock = Lock()

        # Streams to keys map
        self._stream_keys = {}

        # Retry with exponential backoff
        self._retry_max_time = retry_max_time
        self._retry_max_count = retry_max_count

        self._batch_size = batch_size
        self._batch_bytes_size = batch_bytes_size
        self._flush_interval = flush_interval

        # Holds the events after .track method
        self._event_backlog = QueueEventStorage(queue_size=backlog_size)

        # Holds batch of events for each stream and sends them using {thread_count} workers
        self._batch_event_pool = BatchEventPool(thread_count=batch_worker_count,
                                                max_events=batch_pool_size)

        # Start the handler thread - daemon since we want to exit if it didn't stop yet
        handler_thread = Thread(target=self._tracker_handler)
        handler_thread.daemon = True
        handler_thread.start()

        timer_thread = Thread(target=self._flush_peroidcly)
        timer_thread.daemon = True
        timer_thread.start()

        # Intercept exit signals
        signal.signal(signal.SIGTERM, self.graceful_kill)
        signal.signal(signal.SIGINT, self.graceful_kill)

    def stop(self):
        """
        Stop worker thread and event_pool thread's
        """
        self._logger.info("Flushing all data and killing the tracker...")
        self._is_flush_all = True
        i = 0
        while True:
            # Check if everything is empty or 5 seconds has passed
            if self._batch_event_pool.is_empty() and self._event_backlog.is_empty() or i == 5:
                self._logger.warning("BatchPool and Backlog are empty or 5 seconds have passed, killing the tracker")
                self._is_run_worker = False
                self._batch_event_pool.stop()
                break
            i += 1
            time.sleep(1)

    def set_event_manager(self, event_manager):
        """
        Set custom event manager object

        :param event_manager: custom event manager for storage data
        :type event_manager: EventManager
        """
        self._event_backlog = event_manager

    def enable_debug(self, is_debug):  # pragma: no cover
        """
        Enable / Disable debug - this is here for compatibility reasons

        :param is_debug: enable printing of debug info
        :type is_debug: bool
        """
        self.set_debug(is_debug)

    def set_debug(self, is_debug):  # pragma: no cover
        """
        Enable / Disable debug

        :param is_debug: enable printing of debug info
        :type is_debug: bool
        """
        self._is_debug = is_debug if isinstance(is_debug, bool) else False
        self._logger = logger.get_logger(debug=is_debug)

    def set_endpoint(self, endpoint):
        """
        Set server host address

        :param endpoint: server url
        :type endpoint: str
        """
        self._atom.set_endpoint(endpoint)

    def set_auth(self, auth_key):
        """
        Set auth key for stream

        :param auth_key: secret auth key
        :type auth_key: str
        """
        self._atom.set_auth(auth_key)

    def set_bulk_size(self, batch_size):
        """
        Set batch count (this function is here for compatibility reasons)
        :param batch_size: count of batch (bulk) events
        :type batch_size: int
        """
        self.set_batch_size(batch_size)

    def set_batch_size(self, batch_size):
        """
        Set batch count
        :param batch_size: count of batch(bulk) events
        :type batch_size: int
        """
        self._batch_size = batch_size

    def set_bulk_bytes_size(self, batch_bytes_size):
        """
        Set batch bytes size (this function is here for compatibility reasons)

        :param batch_bytes_size: batch(bulk) size in bytes
        :type batch_bytes_size: int
        """
        self.set_batch_bytes_size(batch_bytes_size)

    def set_batch_bytes_size(self, batch_bytes_size):
        """
        Set bulk bytes size

        :param batch_bytes_size: bulk size in bytes
        :type batch_bytes_size: int
        """
        self._batch_bytes_size = batch_bytes_size

    def set_flush_interval(self, flush_interval):
        """
        Set flush interval milliseconds

        :param flush_interval: interval for flush data
        :type flush_interval: int
        """
        self._flush_interval = flush_interval

    def track(self, stream, data, auth_key=""):
        """
        Track event

        :param stream: name of stream
        :type stream: str
        :param data: data for sending
        :type data: object
        :param auth_key: secret auth key for stream
        :type auth_key: str
        """
        if len(auth_key) == 0:
            auth_key = self._atom.get_auth()

        if not isinstance(data, str):
            data = json.dumps(data)

        with self._data_lock:
            if stream not in self._stream_keys:
                self._stream_keys[stream] = auth_key

            self._event_backlog.add_event(Event(stream, data))

    def flush(self):
        """
        Flush data from all streams
        """
        self._is_flush_data = True

    def _flush_peroidcly(self):
        """
        Flush everything every {flush_interval}
        Note: the time.time() is used cause python is not accurate enough and adds a delay
        when using time.sleep(x) (where x is a constant)
        """
        next_call = time.time()
        i = 0
        while self._is_run_worker:
            if i == 10000:
                i = 0
            # Divide by 1000 since flush_interval is provided in milliseconds
            next_call += self._flush_interval / 1000
            # This part is here only for better debugging
            if i % 2 == 0:
                self._logger.debug("Flushing In {} Seconds".format(next_call - time.time()))
            i += 1
            time.sleep(next_call - time.time())
            self._is_flush_all = True

    def _tracker_handler(self):
        """
        Main tracker function, handles flushing based on given conditions
        """
        # Buffer between backlog and batch pool
        events_buffer = {}
        # Dict to hold events size for every stream
        events_size = {}

        def flush_data(stream, auth_key):
            # This 'if' is needed for the flush_all method
            if stream in events_buffer and len(events_buffer[stream]) > 0:
                temp_buffer = list(events_buffer[stream])
                del events_buffer[stream][:]
                events_size[stream] = 0
                self._batch_event_pool.add_event(lambda: self._flush_data(stream, auth_key, temp_buffer))

        while self._is_run_worker:
            if self._is_flush_all:
                for stream_name, stream_key in self._stream_keys.items():
                    flush_data(stream_name, stream_key)
                self._is_flush_all = False
            else:
                for stream_name, stream_key in self._stream_keys.items():
                    # Get one event from the backlog
                    event_object = self._event_backlog.get_event(stream_name)
                    if event_object is None:
                        continue

                    if stream_name not in events_size:
                        events_size[stream_name] = 0

                    if stream_name not in events_buffer:
                        events_buffer[stream_name] = []

                    events_size[stream_name] += len(event_object.data.encode("utf8"))
                    events_buffer[stream_name].append(event_object.data)

                    if events_size[stream_name] >= self._batch_bytes_size:
                        flush_data(stream_name, auth_key=stream_key)

                    if len(events_buffer[stream_name]) >= self._batch_size:
                        flush_data(stream_name, auth_key=stream_key)

                    if self._is_flush_data:
                        flush_data(stream_name, auth_key=stream_key)

                if self._is_flush_data:
                    self._is_flush_data = False
        self._logger.info("Tracker handler stopped")

    def _flush_data(self, stream, auth_key, data):
        """
        Send data to server using IronSource Atom Low-level API
        NOTE: this function is passed a lambda to the BatchEventPool so it might continue running if it was
        triggered already even after a graceful killing for at least (retry_max_count) times
        """
        attempt = 0

        while attempt < self._retry_max_count:
            try:
                response = self._atom.put_events(stream, data=data, auth_key=auth_key)
            except Exception as e:
                self._error_log(attempt, time.time(), 400, e, data)
                return

            # Response on first try
            if attempt == 0:
                self._logger.debug('Got Status: {}; For Data: {:.100}...'.format(str(response.status), str(data)))

            # Status 200 - OK or 400 - Client Error
            if 200 <= response.status < 500:
                if 200 <= response.status < 400:
                    self._logger.info('Status: {}; Response: {}; Error: {}'.format(str(response.status),
                                                                                   str(response.data),
                                                                                   str(response.error)))
                else:
                    # 400
                    self._error_log(attempt, time.time(), response.status, response.error, data)
                return

            # Server Error >= 500
            #  Retry mechanism
            duration = self._get_duration(attempt)
            self._logger.debug("Retry duration: {}".format(duration))
            attempt += 1
            time.sleep(duration)
            self._error_log(attempt, time.time(), response.status, response.error, data)
        # after max attempts reached queue the msgs back to the end of the queue
        else:
            self._logger.warning("Queueing back data after reaching max attempts")
            self._batch_event_pool.add_event(lambda: self._flush_data(stream, auth_key, data))

    def _get_duration(self, attempt):
        """
        Exponential back-off + Full Jitter

        :param attempt: attempt number
        :type attempt: int
        """
        expo_backoff = min(self._retry_max_time, pow(2, attempt) * config.RETRY_EXPO_BACKOFF_BASE)
        return random.uniform(0, expo_backoff)

    def graceful_kill(self, sig, frame):
        """
        Tracker exit handler
        :param frame: current stack frame
        :type frame: frame
        :type sig: OS SIGNAL number
        :param sig: integer
        """
        self._logger.info('Intercepted signal %s' % sig)
        if not self._is_run_worker:
            return
        self.stop()

    def _error_log(self, attempt, unix_time=None, status=None, error_msg=None, sent_data=None):
        """
        Log an error and send it to a callback function (if defined by user)
        :param unix_time: unix(epoch) timestamp
        :param status: HTTP status
        :param error_msg: Error msg from server
        :param sent_data: Data that was sent to server
        """
        try:
            self._callback(unix_time, status, error_msg, sent_data)
        except TypeError as e:
            self._logger.error('Wrong arguments given to callback function: {}'.format(e))

        self._logger.error("Error: {}; Status: {}; Attempt: {}; For Data: {:.50}...".format(error_msg,
                                                                                            status,
                                                                                            attempt,
                                                                                            sent_data))
