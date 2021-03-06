# Copyright (C) 2003-2005 Robey Pointer <robey@lag.net>
#
# This file is part of paramiko.
#
# Paramiko is free software; you can redistribute it and/or modify it under the
# terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# Paramiko is distrubuted in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Paramiko; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA.

"""
L{SFTPFile}
"""

import threading
from paramiko.common import *
from paramiko.sftp import *
from paramiko.file import BufferedFile
from paramiko.sftp_attr import SFTPAttributes


class SFTPFile (BufferedFile):
    """
    Proxy object for a file on the remote server, in client mode SFTP.
    """

    # Some sftp servers will choke if you send read/write requests larger than
    # this size.
    MAX_REQUEST_SIZE = 32768

    def __init__(self, sftp, handle, mode='r', bufsize=-1):
        BufferedFile.__init__(self)
        self.sftp = sftp
        self.handle = handle
        BufferedFile._set_mode(self, mode, bufsize)
        self.pipelined = False
        self._prefetching = False
        self._saved_exception = None

    def __del__(self):
        self.close(_async=True)
        
    def close(self, _async=False):
        # We allow double-close without signaling an error, because real
        # Python file objects do.  However, we must protect against actually
        # sending multiple CMD_CLOSE packets, because after we close our
        # handle, the same handle may be re-allocated by the server, and we
        # may end up mysteriously closing some random other file.  (This is
        # especially important because we unconditionally call close() from
        # __del__.)
        if self._closed:
            return
        if self.pipelined:
            self.sftp._finish_responses(self)
        BufferedFile.close(self)
        try:
            if _async:
                # GC'd file handle could be called from an arbitrary thread -- don't wait for a response
                self.sftp._async_request(type(None), CMD_CLOSE, self.handle)
            else:
                self.sftp._request(CMD_CLOSE, self.handle)
        except EOFError:
            # may have outlived the Transport connection
            pass
        except IOError:
            # may have outlived the Transport connection
            pass

    def _read_prefetch(self, size):
        # while not closed, and haven't fetched past the current position, and haven't reached EOF...
        while (self._prefetch_so_far <= self._realpos) and \
            (self._prefetch_so_far < self._prefetch_size) and not self._closed:
            self.sftp._read_response()
        self._check_exception()
        k = self._prefetch_data.keys()
        k.sort()
        while (len(k) > 0) and (k[0] + len(self._prefetch_data[k[0]]) <= self._realpos):
            # done with that block
            del self._prefetch_data[k[0]]
            k.pop(0)
        if len(k) == 0:
            self._prefetching = False
            return ''
        assert k[0] <= self._realpos
        buf_offset = self._realpos - k[0]
        buf_length = len(self._prefetch_data[k[0]]) - buf_offset
        return self._prefetch_data[k[0]][buf_offset : buf_offset + buf_length]
        
    def _read(self, size):
        size = min(size, self.MAX_REQUEST_SIZE)
        if self._prefetching:
            return self._read_prefetch(size)
        t, msg = self.sftp._request(CMD_READ, self.handle, long(self._realpos), int(size))
        if t != CMD_DATA:
            raise SFTPError('Expected data')
        return msg.get_string()

    def _write(self, data):
        # may write less than requested if it would exceed max packet size
        chunk = min(len(data), self.MAX_REQUEST_SIZE)
        req = self.sftp._async_request(type(None), CMD_WRITE, self.handle, long(self._realpos),
            str(data[:chunk]))
        if not self.pipelined or self.sftp.sock.recv_ready():
            t, msg = self.sftp._read_response(req)
            if t != CMD_STATUS:
                raise SFTPError('Expected status')
            # convert_status already called
        return chunk

    def settimeout(self, timeout):
        """
        Set a timeout on read/write operations on the underlying socket or
        ssh L{Channel}.

        @see: L{Channel.settimeout}
        @param timeout: seconds to wait for a pending read/write operation
            before raising C{socket.timeout}, or C{None} for no timeout
        @type timeout: float
        """
        self.sftp.sock.settimeout(timeout)

    def gettimeout(self):
        """
        Returns the timeout in seconds (as a float) associated with the socket
        or ssh L{Channel} used for this file.

        @see: L{Channel.gettimeout}
        @rtype: float
        """
        return self.sftp.sock.gettimeout()

    def setblocking(self, blocking):
        """
        Set blocking or non-blocking mode on the underiying socket or ssh
        L{Channel}.

        @see: L{Channel.setblocking}
        @param blocking: 0 to set non-blocking mode; non-0 to set blocking
            mode.
        @type blocking: int
        """
        self.sftp.sock.setblocking(blocking)

    def seek(self, offset, whence=0):
        self.flush()
        if whence == self.SEEK_SET:
            self._realpos = self._pos = offset
        elif whence == self.SEEK_CUR:
            self._pos += offset
            self._realpos = self._pos
        else:
            self._realpos = self._pos = self._get_size() + offset
        self._rbuffer = ''

    def stat(self):
        """
        Retrieve information about this file from the remote system.  This is
        exactly like L{SFTP.stat}, except that it operates on an already-open
        file.

        @return: an object containing attributes about this file.
        @rtype: SFTPAttributes
        """
        t, msg = self.sftp._request(CMD_FSTAT, self.handle)
        if t != CMD_ATTRS:
            raise SFTPError('Expected attributes')
        return SFTPAttributes._from_msg(msg)
    
    def check(self, hash_algorithm, offset=0, length=0, block_size=0):
        """
        Ask the server for a hash of a section of this file.  This can be used
        to verify a successful upload or download, or for various rsync-like
        operations.
        
        The file is hashed from C{offset}, for C{length} bytes.  If C{length}
        is 0, the remainder of the file is hashed.  Thus, if both C{offset}
        and C{length} are zero, the entire file is hashed.
        
        Normally, C{block_size} will be 0 (the default), and this method will
        return a byte string representing the requested hash (for example, a
        string of length 16 for MD5, or 20 for SHA-1).  If a non-zero
        C{block_size} is given, each chunk of the file (from C{offset} to
        C{offset + length}) of C{block_size} bytes is computed as a separate
        hash.  The hash results are all concatenated and returned as a single
        string.
        
        For example, C{check('sha1', 0, 1024, 512)} will return a string of
        length 40.  The first 20 bytes will be the SHA-1 of the first 512 bytes
        of the file, and the last 20 bytes will be the SHA-1 of the next 512
        bytes.
        
        @param hash_algorithm: the name of the hash algorithm to use (normally
            C{"sha1"} or C{"md5"})
        @type hash_algorithm: str
        @param offset: offset into the file to begin hashing (0 means to start
            from the beginning)
        @type offset: int or long
        @param length: number of bytes to hash (0 means continue to the end of
            the file)
        @type length: int or long
        @param block_size: number of bytes to hash per result (must not be less
            than 256; 0 means to compute only one hash of the entire segment)
        @type block_size: int
        @return: string of bytes representing the hash of each block,
            concatenated together
        @rtype: str
        
        @note: Many (most?) servers don't support this extension yet.
        
        @raise IOError: if the server doesn't support the "check-file"
            extension, or possibly doesn't support the hash algorithm
            requested
            
        @since: 1.4
        """
        t, msg = self.sftp._request(CMD_EXTENDED, 'check-file', self.handle,
                                    hash_algorithm, long(offset), long(length), block_size)
        ext = msg.get_string()
        alg = msg.get_string()
        data = msg.get_remainder()
        return data
    
    def set_pipelined(self, pipelined=True):
        """
        Turn on/off the pipelining of write operations to this file.  When
        pipelining is on, paramiko won't wait for the server response after
        each write operation.  Instead, they're collected as they come in.
        At the first non-write operation (including L{close}), all remaining
        server responses are collected.  This means that if there was an error
        with one of your later writes, an exception might be thrown from
        within L{close} instead of L{write}.
        
        By default, files are I{not} pipelined.
        
        @param pipelined: C{True} if pipelining should be turned on for this
            file; C{False} otherwise
        @type pipelined: bool
        
        @since: 1.5
        """
        self.pipelined = pipelined
    
    def prefetch(self):
        """
        Pre-fetch the remaining contents of this file in anticipation of
        future L{read} calls.  If reading the entire file, pre-fetching can
        dramatically improve the download speed by avoiding roundtrip latency.
        The file's contents are incrementally buffered in a background thread.
        
        @since: 1.5.1
        """
        size = self.stat().st_size
        # queue up async reads for the rest of the file
        self._prefetching = True
        self._prefetch_so_far = self._realpos
        self._prefetch_size = size
        self._prefetch_data = {}
        t = threading.Thread(target=self._prefetch)
        t.setDaemon(True)
        t.start()
    
    def _prefetch(self):
        n = self._realpos
        size = self._prefetch_size
        while n < size:
            chunk = min(self.MAX_REQUEST_SIZE, size - n)
            self.sftp._async_request(self, CMD_READ, self.handle, long(n), int(chunk))
            n += chunk


    ###  internals...


    def _get_size(self):
        try:
            return self.stat().st_size
        except:
            return 0

    def _async_response(self, t, msg):
        if t == CMD_STATUS:
            # save exception and re-raise it on next file operation
            try:
                self.sftp._convert_status(msg)
            except Exception, x:
                self._saved_exception = x
            return
        if t != CMD_DATA:
            raise SFTPError('Expected data')
        data = msg.get_string()
        self._prefetch_data[self._prefetch_so_far] = data
        self._prefetch_so_far += len(data)
    
    def _check_exception(self):
        "if there's a saved exception, raise & clear it"
        if self._saved_exception is not None:
            x = self._saved_exception
            self._saved_exception = None
            raise x
