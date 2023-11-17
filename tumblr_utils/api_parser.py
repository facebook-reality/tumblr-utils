from typing import Dict, Iterable, Iterator, List, Optional

import http.client
import itertools
import json
import os
import requests
import time

from datetime import datetime, timedelta
from urllib.parse import urlencode

from tumblr_utils.constants import FILE_ENCODING, JSONDict, MAX_POSTS
from tumblr_utils.utils.wget import HTTPError, HTTP_TIMEOUT


class ApiParser:
    TRY_LIMIT = 2
    session: Optional[requests.Session] = None

    def __init__(self, base, account):
        self.base = base
        self.account = account
        self.prev_resps: Optional[List[str]] = None
        self.dashboard_only_blog: Optional[bool] = None
        self._prev_iter: Optional[Iterator[JSONDict]] = None
        self._last_mode: Optional[str] = None
        self._last_offset: Optional[int] = None

    @classmethod
    def setup(cls):
        cls.session = make_requests_session(
            requests.Session, HTTP_RETRY, HTTP_TIMEOUT,
            not options.no_ssl_verify, options.user_agent, options.cookiefile,
        )

    def read_archive(self, prev_archive):
        if options.reuse_json:
            prev_archive = save_folder
        elif prev_archive is None:
            return True

        def read_resp(path):
            with open(path, encoding=FILE_ENCODING) as jf:
                return json.load(jf)

        if options.likes:
            logger.warn('Reading liked timestamps from saved responses (may take a while)\n', account=True)

        if options.idents is None:
            respfiles: Iterable[str] = (
                e.path for e in os.scandir(os.path.join(prev_archive, 'json'))
                if e.name.endswith('.json') and e.is_file()
            )
        else:
            respfiles = []
            for ident in options.idents:
                resp = os.path.join(prev_archive, 'json', str(ident) + '.json')
                if not os.path.isfile(resp):
                    logger.error("post '{}' not found\n".format(ident), account=True)
                    return False
                respfiles.append(resp)

        self.prev_resps = sorted(
            respfiles,
            key=lambda p: (
                read_resp(p)['liked_timestamp'] if options.likes
                else int(os.path.basename(p)[:-5])
            ),
            reverse=True,
        )
        return True

    def _iter_prev(self) -> Iterator[JSONDict]:
        assert self.prev_resps is not None
        for path in self.prev_resps:
            with open(path, encoding=FILE_ENCODING) as f:
                try:
                    yield json.load(f)
                except ValueError as e:
                    f.seek(0)
                    logger.error('{}: {}\n{!r}\n'.format(e.__class__.__name__, e, f.read()))

    def get_initial(self) -> Optional[JSONDict]:
        if self.prev_resps is not None:
            try:
                first_post = next(self._iter_prev())
            except StopIteration:
                return None
            return {'posts': [first_post], 'blog': dict(first_post['blog'], posts=len(self.prev_resps))}

        resp = self.apiparse(1)
        if self.dashboard_only_blog and resp and resp['posts']:
            # svc API doesn't return blog info, steal it from the first post
            resp['blog'] = resp['posts'][0]['blog']
        return resp

    def apiparse(self, count, start=0, before=None, ident=None) -> Optional[JSONDict]:
        if self.prev_resps is not None:
            if self._prev_iter is None:
                self._prev_iter = self._iter_prev()
            if ident is not None:
                assert self._last_mode in (None, 'ident')
                self._last_mode = 'ident'
                # idents are pre-filtered
                try:
                    posts = [next(self._prev_iter)]
                except StopIteration:
                    return None
            else:
                it = self._prev_iter
                if before is not None:
                    assert self._last_mode in (None, 'before')
                    assert self._last_offset is None or before < self._last_offset
                    self._last_mode = 'before'
                    self._last_offset = before
                    it = itertools.dropwhile(
                        lambda p: p['liked_timestamp' if options.likes else 'timestamp'] >= before,
                        it,
                    )
                else:
                    assert self._last_mode in (None, 'offset')
                    assert start == (0 if self._last_offset is None else self._last_offset + MAX_POSTS)
                    self._last_mode = 'offset'
                    self._last_offset = start
                posts = list(itertools.islice(it, None, count))
            return {'posts': posts}

        if self.dashboard_only_blog:
            base = 'https://www.tumblr.com/svc/indash_blog'
            params = {'tumblelog_name_or_id': self.account, 'post_id': '', 'limit': count,
                      'should_bypass_safemode': 'true', 'should_bypass_tagfiltering': 'true'}
            headers: Optional[Dict[str, str]] = {
                'Referer': 'https://www.tumblr.com/dashboard/blog/' + self.account,
                'X-Requested-With': 'XMLHttpRequest',
            }
        else:
            base = self.base
            params = {'api_key': API_KEY, 'limit': count, 'reblog_info': 'true'}
            headers = None
        if ident is not None:
            params['post_id' if self.dashboard_only_blog else 'id'] = ident
        elif before is not None and not self.dashboard_only_blog:
            params['before'] = before
        elif start > 0:
            params['offset'] = start

        try:
            doc, status, reason = self._get_resp(base, params, headers)
        except (OSError, HTTPError) as e:
            logger.error('URL is {}?{}\n[FATAL] Error retrieving API repsonse: {!r}\n'.format(
                base, urlencode(params), e,
            ))
            return None

        if not 200 <= status < 300:
            # Detect dashboard-only blogs by the error codes
            if status == 404 and self.dashboard_only_blog is None and not (doc is None or options.likes):
                errors = doc.get('errors', ())
                if len(errors) == 1 and errors[0].get('code') == 4012:
                    self.dashboard_only_blog = True
                    logger.info('Found dashboard-only blog, trying svc API\n', account=True)
                    return self.apiparse(count, start)  # Recurse once
            if status == 403 and options.likes:
                logger.error('HTTP 403: Most likely {} does not have public likes.\n'.format(self.account))
                return None
            logger.error('URL is {}?{}\n[FATAL] {} API repsonse: HTTP {} {}\n{}'.format(
                base, urlencode(params),
                'Error retrieving' if doc is None else 'Non-OK',
                status, reason,
                '' if doc is None else '{}\n'.format(doc),
            ))
            if status == 401 and self.dashboard_only_blog:
                logger.error("This is a dashboard-only blog, so you probably don't have the right cookies.{}\n".format(
                    '' if options.cookiefile else ' Try --cookiefile.',
                ))
            return None
        if doc is None:
            return None  # OK status but invalid JSON

        if self.dashboard_only_blog:
            with disablens_lock:
                if self.account not in disable_note_scraper:
                    disable_note_scraper.add(self.account)
                    logger.info('[Note Scraper] Dashboard-only blog - scraping disabled for {}\n'.format(self.account))
        elif self.dashboard_only_blog is None:
            # If the first API request succeeds, it's a public blog
            self.dashboard_only_blog = False

        return doc.get('response')

    def _get_resp(self, base, params, headers):
        assert self.session is not None
        try_count = 0
        while True:
            try:
                with self.session.get(base, params=params, headers=headers) as resp:
                    try_count += 1
                    doc = None
                    ctype = resp.headers.get('Content-Type')
                    if not (200 <= resp.status_code < 300 or 400 <= resp.status_code < 500):
                        pass  # Server error, will not attempt to read body
                    elif ctype and ctype.split(';', 1)[0].strip() != 'application/json':
                        logger.error("Unexpected Content-Type: '{}'\n".format(ctype))
                    else:
                        try:
                            doc = resp.json()
                        except ValueError as e:
                            logger.error('{}: {}\n{} {} {}\n{!r}\n'.format(
                                e.__class__.__name__, e, resp.status_code, resp.reason, ctype,
                                resp.content.decode('utf-8'),
                            ))
                    status = resp.status_code if doc is None else doc['meta']['status']
                    if status == 429 and try_count < self.TRY_LIMIT and self._ratelimit_sleep(resp.headers):
                        continue
                    return doc, status, resp.reason if doc is None else http.client.responses.get(status, '(unknown)')
            except HTTPError:
                if not is_dns_working(timeout=5, check=options.use_dns_check):
                    no_internet.signal()
                    continue
                raise

    @staticmethod
    def _ratelimit_sleep(headers):
        # Daily ratelimit
        if headers.get('X-Ratelimit-Perday-Remaining') == '0':
            reset = headers.get('X-Ratelimit-Perday-Reset')
            try:
                freset = float(reset)  # pytype: disable=wrong-arg-types
            except (TypeError, ValueError):
                logger.error("Expected numerical X-Ratelimit-Perday-Reset, got {!r}\n".format(reset))
                msg = 'sometime tomorrow'
            else:
                treset = datetime.now() + timedelta(seconds=freset)
                msg = 'at {}'.format(treset.ctime())
            raise RuntimeError('{}: Daily API ratelimit exceeded. Resume with --continue after reset {}.\n'.format(
                logger.backup_account, msg
            ))

        # Hourly ratelimit
        reset = headers.get('X-Ratelimit-Perhour-Reset')
        if reset is None:
            return False

        try:
            sleep_dur = float(reset)
        except ValueError:
            logger.error("Expected numerical X-Ratelimit-Perhour-Reset, got '{}'\n".format(reset), account=True)
            return False

        hours, remainder = divmod(abs(sleep_dur), 3600)
        minutes, seconds = divmod(remainder, 60)
        sleep_dur_str = ' '.join(str(int(t[0])) + t[1] for t in ((hours, 'h'), (minutes, 'm'), (seconds, 's')) if t[0])

        if sleep_dur < 0:
            logger.warn('Warning: X-Ratelimit-Perhour-Reset is {} in the past\n'.format(sleep_dur_str), account=True)
            return True
        if sleep_dur > 3600:
            treset = datetime.now() + timedelta(seconds=sleep_dur)
            raise RuntimeError('{}: Refusing to sleep for {}. Resume with --continue at {}.'.format(
                logger.backup_account, sleep_dur_str, treset.ctime(),
            ))

        logger.warn('Hit hourly ratelimit, sleeping for {} as requested\n'.format(sleep_dur_str), account=True)
        time.sleep(sleep_dur + 1)  # +1 to be sure we're past the reset
        return True
