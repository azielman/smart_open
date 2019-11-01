# -*- coding: utf-8 -*-
#
# Copyright (C) 2019 Radim Rehurek <me@radimrehurek.com>
#
# This code is distributed under the terms and conditions
# from the MIT License (MIT).
#
"""Implements reading and writing to/from Google Cloud Storage."""

import io
import contextlib
import functools
import logging

import six

from google.cloud import storage
from google.cloud import exceptions
import smart_open.bytebuffer

logger = logging.getLogger(__name__)

DEFAULT_MIN_PART_SIZE = 50 * 1024**2
"""Default minimum part size for GCS multipart uploads"""
MIN_MIN_PART_SIZE = 5 * 1024 ** 2
"""The absolute minimum permitted by Amazon."""
READ_BINARY = 'rb'
WRITE_BINARY = 'wb'
MODES = (READ_BINARY, WRITE_BINARY)
"""Allowed I/O modes for working with GCS."""

BINARY_NEWLINE = b'\n'

SUPPORTED_SCHEMES = ('gs',)

DEFAULT_BUFFER_SIZE = 128 * 1024

START = 0
CURRENT = 1
END = 2
WHENCE_CHOICES = [START, CURRENT, END]


def clamp(value, minval, maxval):
    return max(min(value, maxval), minval)


def make_range_string(start, stop=None):
    #
    # https://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.35
    #
    if stop is None:
        return 'bytes=%d-' % start
    return 'bytes=%d-%d' % (start, stop)


def open(
        bucket_id,
        key_id,
        mode,
        version_id=None,
        buffer_size=DEFAULT_BUFFER_SIZE,
        min_part_size=DEFAULT_MIN_PART_SIZE,
        client=None,
        resource_kwargs=None,
        multipart_upload_kwargs=None,
        ):
    """Open an GCS blob for reading or writing.

    Parameters
    ----------
    bucket_id: str
        The name of the bucket this object resides in.
    key_id: str
        The name of the key within the bucket.
    mode: str
        The mode for opening the object.  Must be either "rb" or "wb".
    buffer_size: int, optional
        The buffer size to use when performing I/O.
    min_part_size: int, optional
        The minimum part size for multipart uploads.  For writing only.
    client: object, optional
        The GCS client to use
    resource_kwargs: dict, optional
        Keyword arguments to use when accessing the GCS resource for reading or writing.
    multipart_upload_kwargs: dict, optional
        Additional parameters to pass to boto3's initiate_multipart_upload function.
        For writing only.
    version_id: str, optional
        Version of the object, used when reading object. If None, will fetch the most recent version.

    """
    logger.debug('%r', locals())
    if mode not in MODES:
        raise NotImplementedError('bad mode: %r expected one of %r' % (mode, MODES))

    if resource_kwargs is None:
        resource_kwargs = {}
    if multipart_upload_kwargs is None:
        multipart_upload_kwargs = {}

    if (mode == WRITE_BINARY) and (version_id is not None):
        raise ValueError("version_id must be None when writing")

    if mode == READ_BINARY:
        fileobj = SeekableBufferedInputBase(
            bucket_id,
            key_id,
            version_id=version_id,
            buffer_size=buffer_size,
            client=client,
            resource_kwargs=resource_kwargs,
        )
    elif mode == WRITE_BINARY:
        fileobj = BufferedOutputBase(
            bucket_id,
            key_id,
            min_part_size=min_part_size,
            client=client,
            multipart_upload_kwargs=multipart_upload_kwargs,
            resource_kwargs=resource_kwargs,
        )
    else:
        assert False, 'unexpected mode: %r' % mode
    return fileobj


def _get(gs_blob, version=None, **kwargs):
    if version is not None:
        kwargs['VersionId'] = version
    try:
        return gs_blob.get(**kwargs)
    except exceptions.GoogleCloudError as error:
        raise IOError(
            'unable to access bucket: %r key: %r version: %r error: %s' % (
                gs_blob.bucket_name, gs_blob.key, version, error
            )
        )


class RawReader(object):
    """Read an GCS blob."""
    def __init__(self, gs_blob):
        self.position = 0
        self._object = gs_blob
        self._body = gs_blob.get()['Body']

    def read(self, size=-1):
        if size == -1:
            return self._body.read()
        return self._body.read(size)


class SeekableRawReader(object):
    """Read an GCS blob."""

    def __init__(self, gs_blob, content_length, version_id=None):
        self._object = gs_blob
        self._content_length = content_length
        self._version_id = version_id
        self.seek(0)

    def seek(self, position):
        """Seek to the specified position (byte offset) in the GCS key.

        :param int position: The byte offset from the beginning of the key.
        """
        self._position = position
        range_string = make_range_string(self._position)
        logger.debug('content_length: %r range_string: %r', self._content_length, range_string)

        #
        # Close old body explicitly.
        # When first seek(), self._body is not exist. Catch the exception and do nothing.
        #
        try:
            self._body.close()
        except AttributeError:
            pass

        if position == self._content_length == 0 or position == self._content_length:
            #
            # When reading, we can't seek to the first byte of an empty file.
            # Similarly, we can't seek past the last byte.  Do nothing here.
            #
            self._body = io.BytesIO()
        else:
            self._body = _get(self._object, self._version_id, Range=range_string)['Body']

    def read(self, size=-1):
        if self._position >= self._content_length:
            return b''
        if size == -1:
            binary = self._body.read()
        else:
            binary = self._body.read(size)
        self._position += len(binary)
        return binary


class BufferedInputBase(io.BufferedIOBase):
    def __init__(self, bucket, key, version_id=None, buffer_size=DEFAULT_BUFFER_SIZE,
                 line_terminator=BINARY_NEWLINE):
        gcs = storage.Client()
        self._bucket = gcs.get_bucket(bucket)
        self._blob = self._bucket.blob(key)
        self._version_id = version_id
        self._raw_reader = RawReader(self._blob)
        self._content_length = self._object.content_length
        self._content_length = _get(self._blob, self._version_id)['ContentLength']
        self._current_pos = 0
        self._buffer = smart_open.bytebuffer.ByteBuffer(buffer_size)
        self._eof = False
        self._line_terminator = line_terminator

        #
        # This member is part of the io.BufferedIOBase interface.
        #
        self.raw = None

    #
    # Override some methods from io.IOBase.
    #
    def close(self):
        """Flush and close this stream."""
        logger.debug("close: called")
        self._blob = None

    def readable(self):
        """Return True if the stream can be read from."""
        return True

    def seekable(self):
        return False

    #
    # io.BufferedIOBase methods.
    #
    def detach(self):
        """Unsupported."""
        raise io.UnsupportedOperation

    def read(self, size=-1):
        """Read up to size bytes from the object and return them."""
        if size == 0:
            return b''
        elif size < 0:
            from_buf = self._read_from_buffer()
            self._current_pos = self._content_length
            return from_buf + self._raw_reader.read()

        #
        # Return unused data first
        #
        if len(self._buffer) >= size:
            return self._read_from_buffer(size)

        #
        # If the stream is finished, return what we have.
        #
        if self._eof:
            return self._read_from_buffer()

        #
        # Fill our buffer to the required size.
        #
        # logger.debug('filling %r byte-long buffer up to %r bytes', len(self._buffer), size)
        self._fill_buffer(size)
        return self._read_from_buffer(size)

    def read1(self, size=-1):
        """This is the same as read()."""
        return self.read(size=size)

    def readinto(self, b):
        """Read up to len(b) bytes into b, and return the number of bytes
        read."""
        data = self.read(len(b))
        if not data:
            return 0
        b[:len(data)] = data
        return len(data)

    def readline(self, limit=-1):
        """Read up to and including the next newline.  Returns the bytes read."""
        if limit != -1:
            raise NotImplementedError('limits other than -1 not implemented yet')
        the_line = io.BytesIO()
        while not (self._eof and len(self._buffer) == 0):
            #
            # In the worst case, we're reading the unread part of self._buffer
            # twice here, once in the if condition and once when calling index.
            #
            # This is sub-optimal, but better than the alternative: wrapping
            # .index in a try..except, because that is slower.
            #
            remaining_buffer = self._buffer.peek()
            if self._line_terminator in remaining_buffer:
                next_newline = remaining_buffer.index(self._line_terminator)
                the_line.write(self._read_from_buffer(next_newline + 1))
                break
            else:
                the_line.write(self._read_from_buffer())
                self._fill_buffer()
        return the_line.getvalue()

    def terminate(self):
        """Do nothing."""
        pass

    #
    # Internal methods.
    #
    def _read_from_buffer(self, size=-1):
        """Remove at most size bytes from our buffer and return them."""
        # logger.debug('reading %r bytes from %r byte-long buffer', size, len(self._buffer))
        size = size if size >= 0 else len(self._buffer)
        part = self._buffer.read(size)
        self._current_pos += len(part)
        # logger.debug('part: %r', part)
        return part

    def _fill_buffer(self, size=-1):
        size = size if size >= 0 else self._buffer._chunk_size
        while len(self._buffer) < size and not self._eof:
            bytes_read = self._buffer.fill(self._raw_reader)
            if bytes_read == 0:
                logger.debug('reached EOF while filling buffer')
                self._eof = True


class SeekableBufferedInputBase(BufferedInputBase):
    """Reads bytes from GCS.

    Implements the io.BufferedIOBase interface of the standard library."""

    def __init__(self, bucket, key, version_id=None, buffer_size=DEFAULT_BUFFER_SIZE,
                 line_terminator=BINARY_NEWLINE):
        gcs = storage.Client()

        self._buffer_size = buffer_size
        self._bucket = gcs.get_bucket(bucket)
        self._blob = self._bucket.blob(key)
        self._version_id = version_id
        self._content_length = _get(self._object, self._version_id)['ContentLength']

        self._raw_reader = SeekableRawReader(self._object, self._content_length, self._version_id)
        self._current_pos = 0
        self._buffer = smart_open.bytebuffer.ByteBuffer(buffer_size)
        self._eof = False
        self._line_terminator = line_terminator

        #
        # This member is part of the io.BufferedIOBase interface.
        #
        self.raw = None

    def seekable(self):
        """If False, seek(), tell() and truncate() will raise IOError.

        We offer only seek support, and no truncate support."""
        return True

    def seek(self, offset, whence=START):
        """Seek to the specified position.

        :param int offset: The offset in bytes.
        :param int whence: Where the offset is from.

        Returns the position after seeking."""
        logger.debug('seeking to offset: %r whence: %r', offset, whence)
        if whence not in WHENCE_CHOICES:
            raise ValueError('invalid whence, expected one of %r' % WHENCE_CHOICES)

        if whence == START:
            new_position = offset
        elif whence == CURRENT:
            new_position = self._current_pos + offset
        else:
            new_position = self._content_length + offset
        new_position = clamp(new_position, 0, self._content_length)
        self._current_pos = new_position
        self._raw_reader.seek(new_position)
        logger.debug('new_position: %r', self._current_pos)

        self._buffer.empty()
        self._eof = self._current_pos == self._content_length
        return self._current_pos

    def tell(self):
        """Return the current position within the file."""
        return self._current_pos

    def truncate(self, size=None):
        """Unsupported."""
        raise io.UnsupportedOperation

    def __str__(self):
        return "smart_open.gcs.SeekableBufferedInputBase(%r, %r)" % (
            self._object.bucket_name, self._object.key
        )

    def __repr__(self):
        return (
            "smart_open.gcs.SeekableBufferedInputBase("
            "bucket=%r, "
            "key=%r, "
            "version_id=%r, "
            "buffer_size=%r, "
            "line_terminator=%r, "
            "session=%r, "
            "resource_kwargs=%r)"
        ) % (
            self._object.bucket_name,
            self._object.key,
            self._version_id,
            self._buffer_size,
            self._line_terminator,
            self._session,
            self._resource_kwargs,
        )


class BufferedOutputBase(io.BufferedIOBase):
    """Writes bytes to GCS.

    Implements the io.BufferedIOBase interface of the standard library."""

    def __init__(
            self,
            bucket,
            key,
            min_part_size=DEFAULT_MIN_PART_SIZE,
            session=None,
            resource_kwargs=None,
            multipart_upload_kwargs=None,
            ):

        self._session = session
        self._resource_kwargs = resource_kwargs
        self._multipart_upload_kwargs = multipart_upload_kwargs
        gcs = storage.Client()
        if session is None:
            session = gcs.create_resumable_upload_session(**session_kwargs)
        if resource_kwargs is None:
            resource_kwargs = {}
        if multipart_upload_kwargs is None:
            multipart_upload_kwargs = {}


        try:
            self._blob = s3.Object(bucket, key)
            self._min_part_size = min_part_size
            self._mp = self._blob.initiate_multipart_upload(**multipart_upload_kwargs)
        except google.cloud.exceptions.GoogleCloudError:
            raise ValueError('the bucket %r does not exist, or is forbidden for access' % bucket)

        self._buf = io.BytesIO()
        self._total_bytes = 0
        self._total_parts = 0
        self._parts = []

        #
        # This member is part of the io.BufferedIOBase interface.
        #
        self.raw = None

    def flush(self):
        pass

    #
    # Override some methods from io.IOBase.
    #
    def close(self):
        logger.debug("closing")
        if self._buf.tell():
            self._upload_next_part()

        if self._total_bytes and self._mp:
            self._mp.complete(MultipartUpload={'Parts': self._parts})
            logger.debug("completed multipart upload")
        elif self._mp:
            #
            # AWS complains with "The XML you provided was not well-formed or
            # did not validate against our published schema" when the input is
            # completely empty => abort the upload, no file created.
            #
            # We work around this by creating an empty file explicitly.
            #
            logger.info("empty input, ignoring multipart upload")
            assert self._mp, "no multipart upload in progress"
            self._mp.abort()

            self._blob.put(Body=b'')
        self._mp = None
        logger.debug("successfully closed")

    @property
    def closed(self):
        return self._mp is None

    def writable(self):
        """Return True if the stream supports writing."""
        return True

    def tell(self):
        """Return the current stream position."""
        return self._total_bytes

    #
    # io.BufferedIOBase methods.
    #
    def detach(self):
        raise io.UnsupportedOperation("detach() not supported")

    def write(self, b):
        """Write the given buffer (bytes, bytearray, memoryview or any buffer
        interface implementation) to the GCS blob.

        For more information about buffers, see https://docs.python.org/3/c-api/buffer.html

        There's buffering happening under the covers, so this may not actually
        do any HTTP transfer right away."""

        length = self._buf.write(b)
        self._total_bytes += length

        if self._buf.tell() >= self._min_part_size:
            self._upload_next_part()

        return length

    def terminate(self):
        """Cancel the underlying multipart upload."""
        assert self._mp, "no multipart upload in progress"
        self._mp.abort()
        self._mp = None

    #
    # Internal methods.
    #
    def _upload_next_part(self):
        part_num = self._total_parts + 1
        logger.info("uploading part #%i, %i bytes (total %.3fGB)",
                    part_num, self._buf.tell(), self._total_bytes / 1024.0 ** 3)
        self._buf.seek(0)
        part = self._mp.Part(part_num)
        upload = part.upload(Body=self._buf)
        self._parts.append({'ETag': upload['ETag'], 'PartNumber': part_num})
        logger.debug("upload of part #%i finished" % part_num)

        self._total_parts += 1
        self._buf = io.BytesIO()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.terminate()
        else:
            self.close()

    def __str__(self):
        return "smart_open.gcs.BufferedOutputBase(%r, %r)" % (self._object.bucket_name, self._object.key)

    def __repr__(self):
        return (
            "smart_open.gcs.BufferedOutputBase("
            "bucket=%r, "
            "key=%r, "
            "min_part_size=%r, "
            "session=%r, "
            "resource_kwargs=%r, "
            "multipart_upload_kwargs=%r)"
        ) % (
            self._object.bucket_name,
            self._object.key,
            self._min_part_size,
            self._session,
            self._resource_kwargs,
            self._multipart_upload_kwargs,
        )


def _accept_all(key):
    return True


def iter_bucket(bucket_name, prefix='', accept_key=None,
                key_limit=None, workers=16, retries=3):
    """
    Iterate and download all GCS blobs under `gs://bucket_name/prefix`.

    Parameters
    ----------
    bucket_name: str
        The name of the bucket.
    prefix: str, optional
        Limits the iteration to keys starting wit the prefix.
    accept_key: callable, optional
        This is a function that accepts a key name (unicode string) and
        returns True/False, signalling whether the given key should be downloaded.
        The default behavior is to accept all keys.
    key_limit: int, optional
        If specified, the iterator will stop after yielding this many results.
    workers: int, optional
        The number of subprocesses to use.
    retries: int, optional
        The number of time to retry a failed download.

    Yields
    ------
    str
        The full key name (does not include the bucket name).
    bytes
        The full contents of the key.

    Notes
    -----
    The keys are processed in parallel, using `workers` processes (default: 16),
    to speed up downloads greatly. If multiprocessing is not available, thus
    _MULTIPROCESSING is False, this parameter will be ignored.

    Examples
    --------

      >>> # get all JSON files under "mybucket/foo/"
      >>> for key, content in iter_bucket(
      ...         bucket_name, prefix='foo/',
      ...         accept_key=lambda key: key.endswith('.json')):
      ...     print key, len(content)

      >>> # limit to 10k files, using 32 parallel workers (default is 16)
      >>> for key, content in iter_bucket(bucket_name, key_limit=10000, workers=32):
      ...     print key, len(content)
    """
    if accept_key is None:
        accept_key = _accept_all

    #
    # If people insist on giving us bucket instances, silently extract the name
    # before moving on.  Works for boto3 as well as boto.
    #
    try:
        bucket_name = bucket_name.name
    except AttributeError:
        pass

    total_size, key_no = 0, -1
    key_iterator = _list_bucket(bucket_name, prefix=prefix, accept_key=accept_key)
    download_key = functools.partial(_download_key, bucket_name=bucket_name, retries=retries)

    with _create_process_pool(processes=workers) as pool:
        result_iterator = pool.imap_unordered(download_key, key_iterator)
        for key_no, (key, content) in enumerate(result_iterator):
            if True or key_no % 1000 == 0:
                logger.info(
                    "yielding key #%i: %s, size %i (total %.1fMB)",
                    key_no, key, len(content), total_size / 1024.0 ** 2
                )
            yield key, content
            total_size += len(content)

            if key_limit is not None and key_no + 1 >= key_limit:
                # we were asked to output only a limited number of keys => we're done
                break
    logger.info("processed %i keys, total size %i" % (key_no + 1, total_size))


def _list_bucket(bucket_name, prefix='', accept_key=lambda k: True):
    gcs = storage.Client()
    ctoken = None

    while True:
        # list_objects_v2 doesn't like a None value for ContinuationToken
        # so we don't set it if we don't have one.
        if ctoken:
            kwargs = dict(Bucket=bucket_name, Prefix=prefix, ContinuationToken=ctoken)
        else:
            kwargs = dict(Bucket=bucket_name, Prefix=prefix)
        response = client.list_objects_v2(**kwargs)
        try:
            content = response['Contents']
        except KeyError:
            pass
        else:
            for c in content:
                key = c['Key']
                if accept_key(key):
                    yield key
        ctoken = response.get('NextContinuationToken', None)
        if not ctoken:
            break


def _download_key(key_name, bucket_name=None, retries=3):
    if bucket_name is None:
        raise ValueError('bucket_name may not be None')

    #
    # https://geekpete.com/blog/multithreading-boto3/
    #
    gcs = storage.Client()
    bucket = gcs.bucket(bucket_name)

    # Sometimes, https://github.com/boto/boto/issues/2409 can happen
    # because of network issues on either side.
    # Retry up to 3 times to ensure its not a transient issue.
    for x in range(retries + 1):
        try:
            content_bytes = _download_fileobj(bucket, key_name)
        except google.cloud.exceptions.GoogleCloudError:
            # Actually fail on last pass through the loop
            if x == retries:
                raise
            # Otherwise, try again, as this might be a transient timeout
            pass
        else:
            return key_name, content_bytes


def _download_fileobj(bucket, key_name):
    #
    # This is a separate function only because it makes it easier to inject
    # exceptions during tests.
    #
    buf = io.BytesIO()
    bucket.download_fileobj(key_name, buf)
    return buf.getvalue()


class DummyPool(object):
    """A class that mimics multiprocessing.pool.Pool for our purposes."""
    def imap_unordered(self, function, items):
        return six.moves.map(function, items)

    def terminate(self):
        pass


@contextlib.contextmanager
def _create_process_pool(processes=1):
    if _MULTIPROCESSING and processes:
        logger.info("creating pool with %i workers", processes)
        pool = multiprocessing.pool.Pool(processes=processes)
    else:
        logger.info("creating dummy pool")
        pool = DummyPool()
    yield pool
    pool.terminate()