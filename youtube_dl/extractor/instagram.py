from ..utils import (
    ExtractorError,
    float_or_none,
    get_element_by_attribute,
    int_or_none,
    lowercase_escape,
    std_headers,
    try_get,
    url_or_none,
)
from .common import InfoExtractor
from ..compat import compat_str

import re
import json
import hashlib
import time  # добавляем импорт модуля time

# Characters for Instagram base-n encoding (base64 URL-safe)
_ENCODING_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'

def _encode_base_n(num, table=_ENCODING_CHARS):
    """Encode a number in base-n with given character table."""
    if num == 0:
        return table[0]
    base = len(table)
    result = ''
    while num:
        num, rem = divmod(num, base)
        result = table[rem] + result
    return result

def _decode_base_n(string, table=_ENCODING_CHARS):
    """Decode a base-n string to an integer using the given character table."""
    base = len(table)
    result = 0
    for char in string:
        idx = table.find(char)
        if idx == -1:
            raise ValueError('Invalid character %r in base-n string' % char)
        result = result * base + idx
    return result

def _pk_to_id(media_id):
    """Convert a numeric media ID to shortcode (base64-like code)."""
    pk = int(str(media_id).split('_')[0])
    return _encode_base_n(pk, _ENCODING_CHARS)

def _id_to_pk(shortcode):
    """Convert a shortcode to a numeric media ID."""
    if len(shortcode) > 28:
        # remove extra 28 characters for private media
        shortcode = shortcode[:-28]
    return _decode_base_n(shortcode, _ENCODING_CHARS)

class InstagramBaseIE(InfoExtractor):
    _API_BASE_URL = 'https://i.instagram.com/api/v1'
    _LOGIN_URL = 'https://www.instagram.com/accounts/login'

    @property
    def _api_headers(self):
        # Base HTTP headers for Instagram API requests
        return {
            'X-IG-App-ID': '936619743392459',
            'X-ASBD-ID': '198387',
            'X-IG-WWW-Claim': '0',
            'Origin': 'https://www.instagram.com',
            'Accept': '*/*',
        }

    def _timestamp_to_str(self, timestamp):
        """Преобразует UNIX-время в строку даты формата YYYYMMDD."""
        return time.strftime('%Y%m%d', time.gmtime(timestamp))

    def _get_count(self, media, kind, *keys):
        # Extract a like/comment count from media JSON
        if not isinstance(media, dict):
            return None
        for key in keys:
            count = try_get(media, (lambda x, key=key: x.get('edge_media_%s' % key, {}).get('count')), int)
            if count is not None:
                return count
        count = try_get(media, (lambda x, kind=kind: x.get('%ss' % kind, {}).get('count')), int)
        return count

    def _get_dimension(self, name, media, webpage=None):
        # Get dimension (width/height) from media or HTML meta
        value = try_get(media, (lambda x: x.get('dimensions', {}).get(name)), int)
        if value is not None:
            return value
        return int_or_none(self._html_search_meta(['og:video:%s' % name, 'video:%s' % name], webpage or '', default=None))

    def _extract_nodes(self, nodes, is_direct=False):
        # Generate video entries from a list of GraphQL nodes
        entries = []
        for idx, node in enumerate(nodes, start=1):
            if node.get('__typename') != 'GraphVideo' and node.get('is_video') is not True:
                continue
            video_id = node.get('shortcode') or node.get('id')
            if is_direct:
                # Direct video info for sidecar
                video_url = node.get('video_url') or node.get('playback_url')
                if not video_url:
                    continue
                entry = {
                    'id': video_id,
                    'url': video_url,
                    'width': self._get_dimension('width', node),
                    'height': self._get_dimension('height', node),
                    'http_headers': {'Referer': 'https://www.instagram.com/'},
                    'title': node.get('title') or 'Video %d' % idx,
                }
                # Include description if available
                description = try_get(node, (lambda x: x['edge_media_to_caption']['edges'][0]['node']['text']), str)
                if description:
                    entry['description'] = description
                entries.append(entry)
            else:
                if not video_id:
                    continue
                entries.append({
                    '_type': 'url',
                    'ie_key': 'Instagram',
                    'id': video_id,
                    'url': 'https://www.instagram.com/p/%s/' % video_id,
                })
        return entries

class InstagramIOSIE(InfoExtractor):
    IE_DESC = 'iOS instagram:// URL'
    _VALID_URL = r'instagram://(?:media\?id=|post\?id=)(?P<id>\d+)'
    _TEST = {
        'url': 'instagram://media?id=12345678901234567',
        'only_matching': True
    }

    def _real_extract(self, url):
        media_id = self._match_id(url)
        short_id = _pk_to_id(media_id)
        return self.url_result('https://www.instagram.com/tv/%s/' % short_id, 'Instagram', short_id)

class InstagramIE(InstagramBaseIE):
    _VALID_URL = r'(?P<url>https?://(?:www\.)?instagram\.com(?:/(?!share/)[^/?#]+)?/(?:p|tv|reels?(?!/audio/))/(?P<id>[^/?#&]+))'
    _EMBED_REGEX = [r'<iframe[^>]+src=(["\'])(?P<url>(?:https?:)?//(?:www\.)?instagram\.com/p/[^/]+/embed.*?)\1']

    def _real_extract(self, url):
        url_match = re.match(self._VALID_URL, url)
        video_id = url_match.group('id')
        media = {}
        webpage = None

        # Use private API if session cookies (user logged in) are available
        if self._get_cookies(url).get('sessionid'):
            info = self._download_json(
                '%s/media/%s/info/' % (self._API_BASE_URL, _id_to_pk(video_id)), video_id,
                note='Downloading video info (API)', errnote='Video info extraction failed',
                headers=self._api_headers, fatal=False
            )
            info_media = try_get(info, (lambda x: x['items'][0]), dict)
            if info_media:
                media.update(info_media)

        # Not logged in: set up a session to get CSRF token
        self._download_json(
            '%s/web/get_ruling_for_content/?content_type=MEDIA&target_id=%s' % (self._API_BASE_URL, _id_to_pk(video_id)),
            video_id, note='Setting up session', errnote='Session setup failed',
            headers=self._api_headers, fatal=False
        )
        csrf_cookie = self._get_cookies('https://www.instagram.com').get('csrftoken')
        csrf_token = csrf_cookie.value if csrf_cookie else None
        if not csrf_token:
            self.report_warning('Instagram API did not provide a CSRF token', video_id)

        # GraphQL query for post data
        variables = {
            'shortcode': video_id,
            'child_comment_count': 3,
            'fetch_comment_count': 40,
            'parent_comment_count': 24,
            'has_threaded_comments': True,
        }
        # Prepare headers with CSRF token for GraphQL request
        headers = self._api_headers.copy() if hasattr(self._api_headers, 'copy') else dict(self._api_headers)
        headers.update({
            'X-CSRFToken': csrf_token or '',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': url,
        })
        general_info = self._download_json(
            'https://www.instagram.com/graphql/query/', video_id,
            note='Downloading JSON metadata', errnote='Metadata extraction failed', fatal=False,
            headers=headers, query={
                'doc_id': '8845758582119845',
                'variables': json.dumps(variables, separators=(',', ':')),
            }
        )
        if not general_info:
            self.report_warning('General metadata extraction failed (missing some data)', video_id)

        # Download main webpage (to use embed fallback if needed)
        webpage, urlh = self._download_webpage_handle(url, video_id, note='Downloading webpage')
        shared_data = self._search_json(r'window\._sharedData\s*=', webpage, 'shared data', video_id, default={})
        if shared_data and self._LOGIN_URL not in urlh.geturl():
            media.update(
                try_get(shared_data, (lambda x: x['entry_data']['PostPage'][0]['graphql']['shortcode_media']), dict)
                or try_get(shared_data, (lambda x: x['entry_data']['PostPage'][0]['media']), dict)
                or {}
            )
        else:
            self.report_warning('Main page is locked behind login, trying embed page', video_id)
            embed_page = self._download_webpage('%s/embed/' % url, video_id, note='Downloading embed webpage', fatal=False) or ''
            additional_data = self._search_json(
                r'window\.__additionalDataLoaded\s*\(\s*[^,]+,\s*({.+?})\s*\)\s*;',
                embed_page, 'additional data', video_id, default={}
            )
            if not additional_data and not media:
                raise ExtractorError('Login required or content unavailable', expected=True)
            product_item = try_get(additional_data, (lambda x: x['items'][0]), dict)
            if product_item:
                media.update(product_item)
                return self._extract_product(media) if hasattr(self, '_extract_product') else self._extract_product_media(media)
            media.update(
                try_get(additional_data, (lambda x: x['graphql']['shortcode_media']), dict)
                or additional_data.get('shortcode_media', {})
                or {}
            )

        # Merge data from GraphQL (if available)
        xdt_media = try_get(general_info, (lambda x: x['data']['xdt_shortcode_media']), dict) or {}
        if xdt_media:
            media.update(xdt_media)
        else:
            # Handle errors or restricted content
            error_title = try_get(general_info, (lambda x: x['title']), str) or ''
            error_desc = try_get(general_info, (lambda x: x['description']), str) or ''
            error_msg = ('%s: %s' % (error_title, error_desc)) if error_title or error_desc else None
            if error_msg:
                raise ExtractorError(error_msg, expected=True)
            if len(video_id) > 28:
                raise ExtractorError('This content is only available to authenticated users', expected=True)
            raise ExtractorError('Instagram returned no media data; post may be private or unavailable', expected=True)

        # Owner username for title/credit
        username = try_get(media, (lambda x: x['owner']['username']), compat_str) or \
                   self._search_regex(r'"owner"\s*:\s*{\s*"username"\s*:\s*"(.+?)"', webpage, 'username', default=None)
        # Caption/description
        description = try_get(media, (lambda x: x['edge_media_to_caption']['edges'][0]['node']['text']), compat_str) or media.get('caption')
        if not description:
            description = self._search_regex(r'"caption"\s*:\s*"(.+?)"', webpage, 'description', default=None)
        if description is not None:
            description = lowercase_escape(description)

        # Determine video URL or handle multiple media in post
        video_url = media.get('video_url')
        if not video_url:
            # Sidecar (carousel) handling
            children_edges = try_get(media, (lambda x: x['edge_sidecar_to_children']['edges']), list) or []
            if children_edges:
                entries = []
                for edge_num, edge in enumerate(children_edges, start=1):
                    node = try_get(edge, (lambda x: x['node']), dict)
                    if not node:
                        continue
                    node_video_url = url_or_none(node.get('video_url'))
                    if not node_video_url:
                        continue
                    entries.append({
                        'id': node.get('shortcode') or node.get('id'),
                        'title': node.get('title') or 'Video %d' % edge_num,
                        'url': node_video_url,
                        'thumbnail': node.get('display_url'),
                        'duration': float_or_none(node.get('video_duration')),
                        'width': int_or_none(try_get(node, (lambda x: x['dimensions']['width']))),
                        'height': int_or_none(try_get(node, (lambda x: x['dimensions']['height']))),
                        'view_count': int_or_none(node.get('video_view_count')),
                    })
                return self.playlist_result(entries, video_id, 'Post by %s' % username if username else None, description)
            # Fallback: try OpenGraph meta for video
            video_url = self._og_search_video_url(webpage, secure=False)
        if not video_url:
            raise ExtractorError('No video found in this Instagram post', expected=True)

        # Build formats list (progressive and DASH)
        formats = [{
            'url': video_url,
            'width': self._get_dimension('width', media, webpage),
            'height': self._get_dimension('height', media, webpage),
        }]
        dash_manifest = media.get('dash_info', {}).get('video_dash_manifest') or media.get('video_dash_manifest')
        if dash_manifest:
            formats.extend(self._parse_mpd_formats(self._parse_xml(dash_manifest, video_id), mpd_id='dash'))

        # Limited comments from JSON (if present)
        comment_edges = None
        if 'edge_media_to_parent_comment' in media:
            comment_edges = media['edge_media_to_parent_comment'].get('edges')
        elif 'edge_media_to_comment' in media:
            comment_edges = media['edge_media_to_comment'].get('edges')
        elif 'comments' in media:
            comment_edges = media.get('comments').get('nodes') or media.get('comments').get('data') or []
        comments = None
        if comment_edges:
            comments = []
            for c in comment_edges:
                comment_node = c.get('node') or c
                text = comment_node.get('text')
                if not text:
                    continue
                comments.append({
                    'author': try_get(comment_node, (lambda x: x.get('owner', {}).get('username') or x.get('user', {}).get('username'))),
                    'author_id': try_get(comment_node, (lambda x: x.get('owner', {}).get('id') or x.get('user', {}).get('pk'))),
                    'id': comment_node.get('id') or comment_node.get('pk'),
                    'text': text,
                    'timestamp': int_or_none(comment_node.get('created_at') or comment_node.get('created_time')),
                })

        # Counts
        like_count = self._get_count(media, 'like', 'preview_like')
        if like_count is None:
            like_count = int_or_none(self._search_regex(r'data-log-event="likeCountClick"[^>]*>([\d,\.]+)', webpage, 'like count', fatal=False))
        comment_count = self._get_count(media, 'comment', 'preview_comment', 'to_comment', 'to_parent_comment') or media.get('comment_count')

        # Thumbnails
        display_resources = media.get('display_resources') or \
            [{'src': media.get(k)} for k in ('display_src', 'display_url') if media.get(k)] or \
            [{'src': self._og_search_thumbnail(webpage)}]
        thumbnails = [{
            'url': thumb.get('src'),
            'width': thumb.get('config_width') or thumb.get('width'),
            'height': thumb.get('config_height') or thumb.get('height'),
        } for thumb in display_resources if thumb.get('src')]

        # Title: use media title or default "Video by <username>"
        title_str = media.get('title')
        if not title_str:
            title_str = 'Video by %s' % username if username else 'Video'

        return {
            'id': video_id,
            'title': title_str,
            'formats': formats,
            'description': description,
            'duration': float_or_none(media.get('video_duration')),
            'thumbnail': thumbnails[0]['url'] if thumbnails else None,
            'thumbnails': thumbnails,
            'timestamp': int_or_none(media.get('taken_at_timestamp') or media.get('date')),
            'upload_date': None if not media.get('taken_at_timestamp') else self._timestamp_to_str(media.get('taken_at_timestamp')),
            'uploader_id': try_get(media, (lambda x: x['owner']['username']), compat_str),
            'uploader': try_get(media, (lambda x: x['owner']['full_name']), compat_str),
            'channel': username,
            'like_count': like_count,
            'comment_count': comment_count,
            'comments': comments,
            'http_headers': {'Referer': 'https://www.instagram.com/'},
            'ext': 'mp4',
        }

class InstagramPlaylistIE(InfoExtractor):
    # Superclass for profile and hashtag playlists (GraphQL pagination)
    _gis_tmpl = None

    def _parse_graphql(self, webpage, item_id):
        return self._parse_json(
            self._search_regex(r'sharedData\s*=\s*({.+?})\s*;\s*<', webpage, 'data', default='{}'),
            item_id, fatal=False
        )

    def _extract_graphql(self, data, url):
        uploader_id = self._match_id(url)
        csrf_token = try_get(data, (lambda x: x['config']['csrf_token']), compat_str) or ''
        rhx_gis = data.get('rhx_gis') or '3c7ca9dcefcf966d11dacf1f151335e8'
        entries = []
        cursor = ''
        for page_num in range(1, float('inf')):
            if page_num == 1:
                timeline = self._parse_timeline_from(data)
            else:
                variables = {'first': 12, 'after': cursor}
                variables.update(self._query_vars_for(data))
                variables_json = json.dumps(variables, separators=(',', ':'))
                if self._gis_tmpl is not None:
                    base = self._gis_tmpl
                    gis_value = hashlib.md5((base + variables_json).encode('utf-8')).hexdigest() if base else None
                    query_headers = {'X-Instagram-GIS': gis_value} if gis_value else {}
                    new_data = self._download_json(
                        'https://www.instagram.com/graphql/query/', uploader_id,
                        note='Downloading GraphQL page %d' % page_num,
                        headers=query_headers, query={'query_hash': self._QUERY_HASH, 'variables': variables_json}
                    )
                else:
                    # Try possible GIS header templates
                    success = False
                    for gis_template in (rhx_gis, '', '%s:%s' % (rhx_gis, csrf_token), '%s:%s:%s' % (rhx_gis, csrf_token, std_headers.get('User-Agent'))):
                        try:
                            query_headers = {'X-Instagram-GIS': hashlib.md5((gis_template + variables_json).encode('utf-8')).hexdigest()} if gis_template else {}
                            new_data = self._download_json(
                                'https://www.instagram.com/graphql/query/', uploader_id,
                                note='Downloading GraphQL page %d' % page_num,
                                headers=query_headers, query={'query_hash': self._QUERY_HASH, 'variables': variables_json}
                            )
                        except ExtractorError:
                            continue
                        self._gis_tmpl = gis_template  # cache the working template
                        timeline = self._parse_timeline_from(new_data)
                        success = True
                        break
                    if not success:
                        break
            if not timeline:
                break
            edges = try_get(timeline, (lambda x: x['edges']), list) or []
            for edge in edges:
                node = edge.get('node')
                if not node or node.get('is_video') is not True:
                    continue
                shortcode = node.get('shortcode')
                if not shortcode:
                    continue
                entries.append({
                    '_type': 'url',
                    'ie_key': 'Instagram',
                    'id': shortcode,
                    'url': 'https://www.instagram.com/p/%s/' % shortcode,
                })
            page_info = timeline.get('page_info') or {}
            if not page_info.get('has_next_page'):
                break
            cursor = page_info.get('end_cursor')
        return entries

class InstagramUserIE(InstagramPlaylistIE):
    IE_DESC = 'Instagram user profile'
    IE_NAME = 'instagram:user'
    _VALID_URL = r'https?://(?:www\.)?instagram\.com/(?P<id>[^/]{2,})/?(?:$|[?#])'
    _QUERY_HASH = '42323d64886122307be10013ad2dcc44'

    @staticmethod
    def _parse_timeline_from(data):
        return data['data']['user']['edge_owner_to_timeline_media']

    @staticmethod
    def _query_vars_for(data):
        return {
            'id': data['entry_data']['ProfilePage'][0]['graphql']['user']['id']
        }

    def _real_extract(self, url):
        username = self._match_id(url)
        webpage = self._download_webpage(url, username)
        data = self._parse_graphql(webpage, username) or {}
        entries = self._extract_graphql(data, url)
        return self.playlist_result(entries, username, username)

class InstagramTagIE(InstagramPlaylistIE):
    IE_DESC = 'Instagram hashtag search'
    IE_NAME = 'instagram:tag'
    _VALID_URL = r'https?://(?:www\.)?instagram\.com/explore/tags/(?P<id>[^/]+)'
    _QUERY_HASH = 'f92f56d47dc7a55b606908374b43a314'

    @staticmethod
    def _parse_timeline_from(data):
        return data['data']['hashtag']['edge_hashtag_to_media']

    @staticmethod
    def _query_vars_for(data):
        return {
            'tag_name': data['entry_data']['TagPage'][0]['graphql']['hashtag']['name']
        }

    def _real_extract(self, url):
        tag = self._match_id(url)
        webpage = self._download_webpage(url, tag)
        data = self._parse_graphql(webpage, tag) or {}
        entries = self._extract_graphql(data, url)
        return self.playlist_result(entries, tag, tag)
