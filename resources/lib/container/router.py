import sys
import xbmc
import xbmcplugin
import xbmcaddon
from threading import Thread
from resources.lib.addon.constants import NO_LABEL_FORMATTING, RANDOMISED_TRAKT, RANDOMISED_LISTS, TRAKT_LIST_OF_LISTS, TMDB_BASIC_LISTS, TRAKT_BASIC_LISTS, TRAKT_SYNC_LISTS, ROUTE_NO_ID, ROUTE_TMDB_ID
from resources.lib.kodi.rpc import get_kodi_library, get_movie_details, get_tvshow_details, get_episode_details, get_season_details, set_playprogress
from resources.lib.addon.plugin import convert_type, reconfigure_legacy_params
from resources.lib.script.router import related_lists
from resources.lib.container.listitem import ListItem
from resources.lib.tmdb.api import TMDb
from resources.lib.trakt.api import TraktAPI
from resources.lib.fanarttv.api import FanartTV
from resources.lib.omdb.api import OMDb
from resources.lib.player.players import Players
from resources.lib.addon.plugin import kodi_log
from resources.lib.container.basedir import BaseDirLists
from resources.lib.tmdb.lists import TMDbLists
from resources.lib.trakt.lists import TraktLists
from resources.lib.tmdb.search import SearchLists
from resources.lib.tmdb.discover import UserDiscoverLists
from resources.lib.api.mapping import set_show, get_empty_item
from resources.lib.addon.parser import parse_paramstring, try_int
from resources.lib.addon.setutils import split_items, random_from_list, merge_two_dicts
from resources.lib.addon.decorators import TimerList


ADDON = xbmcaddon.Addon('plugin.video.themoviedb.helper')


def filtered_item(item, key, value, exclude=False):
    boolean = False if exclude else True  # Flip values if we want to exclude instead of include
    if key and value and key in item and str(value).lower() in str(item[key]).lower():
        boolean = exclude
    return boolean


class Container(TMDbLists, BaseDirLists, SearchLists, UserDiscoverLists, TraktLists):
    def __init__(self):
        self.handle = int(sys.argv[1])
        self.paramstring = sys.argv[2][1:]
        self.params = parse_paramstring(self.paramstring)
        self.parent_params = self.params
        self.update_listing = False
        self.plugin_category = ''
        self.container_content = ''
        self.container_update = None
        self.container_refresh = False
        self.item_type = None
        self.kodi_db = None
        self.kodi_db_tv = {}
        self.timer_lists = {}
        self.log_timers = ADDON.getSettingBool('timer_reports')
        self.library = None
        self.tmdb_api = TMDb()
        self.trakt_api = TraktAPI()
        self.omdb_api = OMDb() if ADDON.getSettingString('omdb_apikey') else None
        self.is_widget = self.params.pop('widget', '').lower() == 'true'
        self.hide_watched = ADDON.getSettingBool('widgets_hidewatched') if self.is_widget else False
        self.flatten_seasons = ADDON.getSettingBool('flatten_seasons')
        self.trakt_watchedindicators = ADDON.getSettingBool('trakt_watchedindicators')
        self.trakt_playprogress = ADDON.getSettingBool('trakt_playprogress')
        self.cache_only = self.params.pop('cacheonly', '').lower()
        self.ftv_forced_lookup = self.params.pop('fanarttv', '').lower()
        self.ftv_api = FanartTV(cache_only=self.ftv_is_cache_only())  # Set after ftv_forced_lookup, is_widget, cache_only
        self.tmdb_cache_only = self.tmdb_is_cache_only()  # Set after ftv_api, cache_only
        self.filter_key = self.params.get('filter_key', None)
        self.filter_value = split_items(self.params.get('filter_value', None))[0]
        self.exclude_key = self.params.get('exclude_key', None)
        self.exclude_value = split_items(self.params.get('exclude_value', None))[0]
        self.pagination = self.pagination_is_allowed()
        self.params = reconfigure_legacy_params(**self.params)
        self.thumb_override = 0

    def pagination_is_allowed(self):
        if self.params.pop('nextpage', '').lower() == 'false':
            return False
        if self.is_widget and not ADDON.getSettingBool('widgets_nextpage'):
            return False
        return True

    def ftv_is_cache_only(self):
        if self.cache_only == 'true':
            return True
        if self.ftv_forced_lookup == 'true':
            return False
        if self.ftv_forced_lookup == 'false':
            return True
        if self.is_widget and ADDON.getSettingBool('widget_fanarttv_lookup'):
            return False
        if not self.is_widget and ADDON.getSettingBool('fanarttv_lookup'):
            return False
        return True

    def tmdb_is_cache_only(self):
        if self.cache_only == 'true':
            return True
        if self.ftv_api:
            return False
        if ADDON.getSettingBool('tmdb_details'):
            return False
        return True

    def _add_item(self, x, li, cache_only=True, ftv_art=None):
        with TimerList(self.timer_lists, 'item_tmdb', log_threshold=0.05, logging=self.log_timers):
            li.set_details(details=self.get_tmdb_details(li, cache_only=cache_only))
        with TimerList(self.timer_lists, 'item_ftv', log_threshold=0.05, logging=self.log_timers):
            li.set_details(details=ftv_art or self.get_ftv_artwork(li), reverse=True)
        self.items_queue[x] = li

    def add_items(self, items=None, pagination=True, parent_params=None, property_params=None, kodi_db=None, cache_only=True):
        if not items:
            return
        check_is_aired = parent_params.get('info') not in NO_LABEL_FORMATTING
        hide_nodate = ADDON.getSettingBool('nodate_is_unaired')

        # Pre-game details and artwork cache for seasons/episodes before threading to avoid multiple API calls
        ftv_art = None
        if parent_params.get('info') in ['seasons', 'episodes', 'episode_groups', 'trakt_upnext']:
            details = self.tmdb_api.get_details('tv', parent_params.get('tmdb_id'), parent_params.get('season', 0), cache_only=cache_only)
            ftv_art = self.get_ftv_artwork(ListItem(parent_params=parent_params, **details))

        # Build empty queue and thread pool
        self.items_queue, pool = [None] * len(items), [None] * len(items)

        # Start item build threads
        for x, i in enumerate(items):
            if not pagination and 'next_page' in i:
                continue
            li = ListItem(parent_params=parent_params, **i)
            pool[x] = Thread(target=self._add_item, args=[x, li, cache_only, ftv_art])
            pool[x].start()

        # Wait to join threads in pool first before adding item to directory
        all_items = []
        for x, i in enumerate(pool):
            if not i:
                continue
            i.join()
            li = self.items_queue[x]
            if not li:
                continue
            if not li.next_page and self.item_is_excluded(li):
                continue
            all_items.append(li)
        # TODO: Get trakt watched sync in thread beforehand and join here so ready - might save 0.5s
        # Final configuration before adding to directory
        for li in all_items:
            li.set_episode_label()
            if check_is_aired and li.is_unaired(no_date=hide_nodate):
                continue
            with TimerList(self.timer_lists, 'item_kodi', log_threshold=0.05, logging=self.log_timers):
                li.set_details(details=self.get_kodi_details(li), reverse=True)  # Quick because local db
            with TimerList(self.timer_lists, 'item_trakt', log_threshold=0.05, logging=self.log_timers):
                li.set_playcount(playcount=self.get_playcount_from_trakt(li))  # Quick because of agressive caching of Trakt object and pre-emptive dict comprehension
            if self.hide_watched and try_int(li.infolabels.get('playcount')) != 0:
                continue
            with TimerList(self.timer_lists, 'item_build', logging=self.log_timers):
                li.set_context_menu()  # Set the context menu items
                li.set_uids_to_info()  # Add unique ids to properties so accessible in skins
                li.set_thumb_to_art(self.thumb_override == 2) if self.thumb_override else None
                li.set_params_reroute(self.ftv_forced_lookup, self.flatten_seasons, self.params.get('extended'), self.cache_only)  # Reroute details to proper end point
                li.set_params_to_info(self.plugin_category)  # Set path params to properties for use in skins
                li.infoproperties.update(property_params or {})
                if self.thumb_override:
                    li.infolabels.pop('dbid', None)  # Need to pop the DBID if overriding thumb otherwise Kodi overrides after item is created
                if li.next_page:
                    li.params['plugin_category'] = self.plugin_category
                self.set_playprogress_from_trakt(li)
                xbmcplugin.addDirectoryItem(
                    handle=self.handle,
                    url=li.get_url(),
                    listitem=li.get_listitem(),
                    isFolder=li.is_folder)

    def set_params_to_container(self, **kwargs):
        params = {}
        for k, v in kwargs.items():
            if not k or not v:
                continue
            try:
                k = u'Param.{}'.format(k)
                v = u'{}'.format(v)
                params[k] = v
                xbmcplugin.setProperty(self.handle, k, v)  # Set params to container properties
            except Exception as exc:
                kodi_log(u'Error: {}\nUnable to set param {} to {}'.format(exc, k, v), 1)
        return params

    def finish_container(self, update_listing=False, plugin_category='', container_content=''):
        xbmcplugin.setPluginCategory(self.handle, plugin_category)  # Container.PluginCategory
        xbmcplugin.setContent(self.handle, container_content)  # Container.Content
        xbmcplugin.endOfDirectory(self.handle, updateListing=update_listing)

    def item_is_excluded(self, listitem):
        if self.filter_key and self.filter_value:
            if self.filter_value == 'is_empty':
                if listitem.infolabels.get(self.filter_key) or listitem.infoproperties.get(self.filter_key):
                    return True
            elif self.filter_key in listitem.infolabels:
                if filtered_item(listitem.infolabels, self.filter_key, self.filter_value):
                    return True
            elif self.filter_key in listitem.infoproperties:
                if filtered_item(listitem.infoproperties, self.filter_key, self.filter_value):
                    return True
        if self.exclude_key and self.exclude_value:
            if self.exclude_value == 'is_empty':
                if not listitem.infolabels.get(self.exclude_key) and not listitem.infoproperties.get(self.exclude_key):
                    return True
            elif self.exclude_key in listitem.infolabels:
                if filtered_item(listitem.infolabels, self.exclude_key, self.exclude_value, True):
                    return True
            elif self.exclude_key in listitem.infoproperties:
                if filtered_item(listitem.infoproperties, self.exclude_key, self.exclude_value, True):
                    return True

    def get_tmdb_details(self, li, cache_only=True):
        if not self.tmdb_api:
            return
        return self.tmdb_api.get_details(
            li.get_tmdb_type(),
            li.unique_ids.get('tvshow.tmdb') if li.infolabels.get('mediatype') in ['season', 'episode'] else li.unique_ids.get('tmdb'),
            li.infolabels.get('season', 0) if li.infolabels.get('mediatype') in ['season', 'episode'] else None,
            li.infolabels.get('episode') if li.infolabels.get('mediatype') == 'episode' else None,
            cache_only=cache_only)

    def get_ftv_artwork(self, li):
        if not self.ftv_api:
            return
        artwork = self.ftv_api.get_all_artwork(li.get_ftv_id(), li.get_ftv_type())
        if not artwork:
            return
        if li.infolabels.get('mediatype') in ['season', 'episode']:
            artwork = {u'tvshow.{}'.format(k): v for k, v in artwork.items() if v}
        return {'art': artwork}

    def _set_playprogress_from_trakt(self, li):
        if li.infolabels.get('mediatype') == 'movie':
            return self.trakt_api.get_movie_playprogress(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')))
        return self.trakt_api.get_episode_playprogress(
            id_type='tmdb',
            unique_id=try_int(li.unique_ids.get('tmdb')),
            season=li.infolabels.get('season'),
            episode=li.infolabels.get('episode'))

    def set_playprogress_from_trakt(self, li):
        if not self.trakt_playprogress:
            return
        if li.infolabels.get('mediatype') not in ['movie', 'episode']:
            return
        duration = li.infolabels.get('duration')
        if not duration:
            return
        progress = self._set_playprogress_from_trakt(li)
        if not progress:
            return
        set_playprogress(li.get_url(), duration * progress / 100, duration)

    def get_playcount_from_trakt(self, li):
        if not self.trakt_watchedindicators:
            return
        if li.infolabels.get('mediatype') == 'movie':
            return self.trakt_api.get_movie_playcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')))
        if li.infolabels.get('mediatype') == 'episode':
            return self.trakt_api.get_episode_playcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tvshow.tmdb')),
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode'))
        if li.infolabels.get('mediatype') == 'tvshow':
            li.infolabels['episode'] = self.trakt_api.get_episodes_airedcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')))
            return self.trakt_api.get_episodes_watchcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')))
        if li.infolabels.get('mediatype') == 'season':
            li.infolabels['episode'] = self.trakt_api.get_episodes_airedcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')),
                season=li.infolabels.get('season'))
            return self.trakt_api.get_episodes_watchcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')),
                season=li.infolabels.get('season'))

    def get_kodi_database(self, tmdb_type):
        with TimerList(self.timer_lists, ' - kodi_db', logging=self.log_timers):
            if ADDON.getSettingBool('local_db'):
                return get_kodi_library(tmdb_type)

    def get_kodi_parent_dbid(self, li):
        if not self.kodi_db:
            return
        if li.infolabels.get('mediatype') in ['movie', 'tvshow']:
            return self.kodi_db.get_info(
                info='dbid',
                imdb_id=li.unique_ids.get('imdb'),
                tmdb_id=li.unique_ids.get('tmdb'),
                tvdb_id=li.unique_ids.get('tvdb'),
                originaltitle=li.infolabels.get('originaltitle'),
                title=li.infolabels.get('title'),
                year=li.infolabels.get('year'))
        if li.infolabels.get('mediatype') in ['season', 'episode']:
            return self.kodi_db.get_info(
                info='dbid',
                imdb_id=li.unique_ids.get('tvshow.imdb'),
                tmdb_id=li.unique_ids.get('tvshow.tmdb'),
                tvdb_id=li.unique_ids.get('tvshow.tvdb'),
                title=li.infolabels.get('tvshowtitle'))

    def get_kodi_details(self, li):
        if not self.kodi_db:
            return
        dbid = self.get_kodi_parent_dbid(li)
        if not dbid:
            return
        if li.infolabels.get('mediatype') == 'movie':
            return get_movie_details(dbid)
        if li.infolabels.get('mediatype') == 'tvshow':
            return get_tvshow_details(dbid)
        if li.infolabels.get('mediatype') == 'season':
            return set_show(self.get_kodi_tvchild_details(
                tvshowid=dbid,
                season=li.infolabels.get('season'),
                is_season=True) or get_empty_item(), get_tvshow_details(dbid))
        if li.infolabels.get('mediatype') == 'episode':
            return set_show(self.get_kodi_tvchild_details(
                tvshowid=dbid,
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode')) or get_empty_item(), get_tvshow_details(dbid))

    def get_kodi_tvchild_details(self, tvshowid, season=None, episode=None, is_season=False):
        if not tvshowid or not season or (not episode and not is_season):
            return
        library = 'season' if is_season else 'episode'
        self.kodi_db_tv[tvshowid] = self.kodi_db_tv.get(tvshowid) or get_kodi_library(library, tvshowid)
        if not self.kodi_db_tv[tvshowid].database:
            return
        dbid = self.kodi_db_tv[tvshowid].get_info('dbid', season=season, episode=episode)
        if not dbid:
            return
        details = get_season_details(dbid) if is_season else get_episode_details(dbid)
        details['infoproperties']['tvshow.dbid'] = tvshowid
        return details

    def get_container_content(self, tmdb_type, season=None, episode=None):
        if tmdb_type == 'tv' and season and episode:
            return convert_type('episode', 'container')
        elif tmdb_type == 'tv' and season:
            return convert_type('season', 'container')
        return convert_type(tmdb_type, 'container')

    def list_randomised_trakt(self, **kwargs):
        kwargs['info'] = RANDOMISED_TRAKT.get(kwargs.get('info'), {}).get('info')
        kwargs['randomise'] = True
        self.parent_params = kwargs
        return self.get_items(**kwargs)

    def list_randomised(self, **kwargs):
        params = merge_two_dicts(kwargs, RANDOMISED_LISTS.get(kwargs.get('info'), {}).get('params'))
        item = random_from_list(self.get_items(**params))
        if not item:
            return
        self.plugin_category = '{}'.format(item.get('label'))
        self.parent_params = item.get('params', {})
        return self.get_items(**item.get('params', {}))

    def get_tmdb_id(self, info, **kwargs):
        if info == 'collection':
            kwargs['tmdb_type'] = 'collection'
        return self.tmdb_api.get_tmdb_id(**kwargs)

    def _noop(self):
        return None

    def _get_items(self, func, **kwargs):
        return func['lambda'](getattr(self, func['getattr']), **kwargs)

    def get_items(self, **kwargs):
        info = kwargs.get('info')

        # Check routes that don't require ID lookups first
        route = ROUTE_NO_ID
        route.update(TRAKT_LIST_OF_LISTS)
        route.update(RANDOMISED_LISTS)
        route.update(RANDOMISED_TRAKT)

        # Early exit if we have a route
        if info in route:
            return self._get_items(route[info]['route'], **kwargs)

        # Check routes that require ID lookups second
        route = ROUTE_TMDB_ID
        route.update(TMDB_BASIC_LISTS)
        route.update(TRAKT_BASIC_LISTS)
        route.update(TRAKT_SYNC_LISTS)

        # Early exit to basedir if no route found
        if info not in route:
            return self.list_basedir(info)

        # Lookup up our TMDb ID
        if not kwargs.get('tmdb_id'):
            self.parent_params['tmdb_id'] = self.params['tmdb_id'] = kwargs['tmdb_id'] = self.get_tmdb_id(**kwargs)

        return self._get_items(route[info]['route'], **kwargs)

    def log_timer_report(self):
        total_log = self.timer_lists.pop('total', 0)
        timer_log = ['DIRECTORY TIMER REPORT\n', self.paramstring, '\n']
        timer_log.append('------------------------------\n')
        for k, v in self.timer_lists.items():
            if k in ['item_tmdb', 'item_ftv']:
                avg_time = u'{:7.3f} sec avg | {:7.3f} sec max | {:3}'.format(sum(v) / len(v), max(v), len(v)) if v else '  None'
                timer_log.append(' - {:12s}: {}\n'.format(k, avg_time))
            elif k[:4] == 'item':
                avg_time = u'{:7.3f} sec avg | {:7.3f} sec all | {:3}'.format(sum(v) / len(v), sum(v), len(v)) if v else '  None'
                timer_log.append(' - {:12s}: {}\n'.format(k, avg_time))
            else:
                tot_time = u'{:7.3f} sec'.format(sum(v) / len(v)) if v else '  None'
                timer_log.append('{:15s}: {}\n'.format(k, tot_time))
        timer_log.append('------------------------------\n')
        tot_time = u'{:7.3f} sec'.format(sum(total_log) / len(total_log)) if total_log else '  None'
        timer_log.append('{:15s}: {}\n'.format('Total', tot_time))
        for k, v in self.timer_lists.items():
            if v and k in ['item_tmdb', 'item_ftv']:
                timer_log.append('\n{}:\n{}\n'.format(k, ' '.join([u'{:.3f} '.format(i) for i in v])))
        kodi_log(timer_log, 1)

    def get_directory(self):
        with TimerList(self.timer_lists, 'total', logging=self.log_timers):
            with TimerList(self.timer_lists, 'get_list', logging=self.log_timers):
                items = self.get_items(**self.params)
            if not items:
                return
            self.plugin_category = self.params.get('plugin_category') or self.plugin_category
            with TimerList(self.timer_lists, 'add_items', logging=self.log_timers):
                self.add_items(
                    items,
                    pagination=self.pagination,
                    parent_params=self.parent_params,
                    property_params=self.set_params_to_container(**self.params),
                    kodi_db=self.kodi_db,
                    cache_only=self.tmdb_cache_only)
            self.finish_container(
                update_listing=self.update_listing,
                plugin_category=self.plugin_category,
                container_content=self.container_content)
        if self.log_timers:
            self.log_timer_report()
        if self.container_update:
            xbmc.executebuiltin(u'Container.Update({})'.format(self.container_update))
        if self.container_refresh:
            xbmc.executebuiltin('Container.Refresh')

    def play_external(self, **kwargs):
        kodi_log(['lib.container.router - Attempting to play item\n', kwargs], 1)
        if not kwargs.get('tmdb_id'):
            kwargs['tmdb_id'] = self.tmdb_api.get_tmdb_id(**kwargs)
        Players(**kwargs).play(handle=self.handle if self.handle != -1 else None)

    def context_related(self, **kwargs):
        if not kwargs.get('tmdb_id'):
            kwargs['tmdb_id'] = self.tmdb_api.get_tmdb_id(**kwargs)
        kwargs['container_update'] = True
        related_lists(include_play=True, **kwargs)

    def router(self):
        if self.params.get('info') == 'play':
            return self.play_external(**self.params)
        if self.params.get('info') == 'related':
            return self.context_related(**self.params)
        return self.get_directory()
