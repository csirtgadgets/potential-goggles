import logging
import subprocess
import os
from pprint import pprint
from csirtg_smrt.constants import VERSION, SMRT_CACHE
from datetime import datetime
import magic
import re
import sys
from time import sleep
import arrow
import requests


RE_SUPPORTED_DECODE = re.compile("zip|lzf|lzma|xz|lzop")
RE_CACHE_TYPES = re.compile('([\w.-]+\.(csv|zip|txt|gz))$')
RE_FQDN = r'((?!-))(xn--)?[a-z0-9][a-z0-9-_\.]{0,245}[a-z0-9]{0,1}\.(xn--)?([a-z0-9\-]{1,61}|[a-z0-9-]{1,30}\.[a-z]{2,})'

FETCHER_TIMEOUT = os.getenv('CSIRTG_SMRT_FETCHER_TIMEOUT', 120)
RETRIES = os.getenv('CSIRTG_SMRT_FETCHER_RETRIES', 3)
RETRIES_DELAY = os.getenv('CSIRTG_SMRT_FETCHER_RETRY_DELAY', 30)  # seconds
NO_HEAD = os.getenv('CSIRTG_SMRT_FETCHER_NOHEAD')

logging.getLogger('requests.packages.urllib3.connectionpool').setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)


class Client(object):

    def __init__(self, rule, feed, **kwargs):

        self.feed = feed
        self.rule = rule
        self.cache = kwargs.get('cache', SMRT_CACHE)
        self.timeout = FETCHER_TIMEOUT
        self.verify_ssl = kwargs.get('verify_ssl', True)

        self.handle = requests.session()
        self.handle.headers['User-Agent'] = "csirtg-smrt/{0} (csirtgadgets.com)".format(VERSION)
        self.handle.headers['Accept'] = 'application/json'

        self.provider = self.rule.get('provider')

        self._init_remote(feed)
        self._init_paths(feed)

    def _init_remote(self, feed):
        if self.rule.remote:
            self.remote = self.rule.remote
        elif self.rule.defaults and self.rule.defaults.get('remote'):
            self.remote = self.rule.defaults.get('remote')
        else:
            self.remote = self.rule.feeds[feed].get('remote')

        self.username = None
        self.password = None

        if not self.provider:
            match = re.search(RE_FQDN, self.remote)
            self.provider = match[0]

            if not self.rule.defaults:
                self.rule.defaults = {}

            if not self.rule.defaults.get('provider'):
                self.rule.defaults['provider'] = match[0]

    def _init_paths(self, feed):
        self.dir = os.path.join(self.cache, self.provider)
        logger.debug(self.dir)

        if not os.path.exists(self.dir):
            try:
                os.makedirs(self.dir)
            except OSError:
                logger.critical('failed to create {0}'.format(self.dir))
                raise

        if self.rule.feeds[feed].get('cache'):
            self.cache = os.path.join(self.dir, self.rule.feeds[feed]['cache'])
            self.cache_file = True

        elif self.remote and RE_CACHE_TYPES.search(self.remote):
            self.cache = RE_CACHE_TYPES.search(self.remote).groups()
            self.cache = os.path.join(self.dir, self.cache[0])
            self.cache_file = True

        else:
            self.cache = os.path.join(self.dir, self.feed)

        logger.debug('CACHE %s' % self.cache)

    def _cache_modified(self):
        ts = os.stat(self.cache)
        ts = arrow.get(ts.st_mtime)
        return ts

    def _cache_size(self):
        if not os.path.isfile(self.cache):
            return 0

        s = os.stat(self.cache)
        return s.st_size

    def _cache_refresh(self, s, auth):
        resp = s.get(self.remote, stream=True, auth=auth, timeout=self.timeout, verify=self.verify_ssl)

        if resp.status_code == 200:
            return resp

        if resp.status_code not in [429, 500, 502, 503, 504]:
            return

        n = RETRIES
        retry_delay = RETRIES_DELAY
        while n != 0:
            if resp.status_code == 429:
                logger.info('Rate Limit Exceeded, retrying in %ss' % retry_delay)
            else:
                logger.error('%s found, retrying in %ss' % (resp.status_code, retry_delay))

            sleep(retry_delay)
            resp = s.get(self.remote, stream=True, auth=auth, timeout=self.timeout,
                         verify=self.verify_ssl)
            if resp.status_code == 200:
                return resp

            n -= 1

    def _cache_write(self, s):
        with open(self.cache, 'wb') as f:
            auth = False
            if self.username:
                auth = (self.username, self.password)

            resp = self._cache_refresh(s, auth)
            if not resp:
                return

            for block in resp.iter_content(1024):
                f.write(block)

    def _cache_decode(self):
        from ..utils.content import get_mimetype
        from ..utils.decoders import decompress_gzip, decompress_zip
        ftype = get_mimetype(self.cache)
        if ftype.startswith('application/x-gzip') or ftype.startswith('application/gzip'):
            self.cache = decompress_gzip(self.cache)

        elif ftype == "application/zip":
            self.cache = decompress_zip(self.cache)

    def fetch(self):
        if self._cache_size() == 0:
            logger.debug('cache size is 0, downloading...')
            self._cache_write(self.handle)
            return

        logger.debug('checking HEAD')

        auth = False
        if self.username:
            auth = (self.username, self.password)

        resp = self.handle.head(self.remote, auth=auth, verify=self.verify_ssl)

        if not resp.headers.get('Last-Modified'):
            logger.debug('no last-modified header')
            self._cache_write(self.handle)
            return

        ts = resp.headers.get('Last-Modified')

        ts1 = arrow.get(datetime.strptime(ts, '%a, %d %b %Y %X %Z'))
        ts2 = self._cache_modified()

        if not NO_HEAD and (ts1 <= ts2):
            logger.debug('cache is OK: {} <= {}'.format(ts1, ts2))
            return

        logger.debug("refreshing cache...")
        self._cache_write(self.handle)
