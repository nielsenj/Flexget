import logging
from datetime import datetime
from manager import ModuleWarning

log = logging.getLogger('feed')

class Entry(dict):

    """
        Represents one item in feed. Must have url and title fields.
        See. http://flexget.com/wiki/DevelopersEntry

        Internally stored original_url is necessary because
        modules (ie. resolvers) may change this into something else 
        and otherwise that information would be lost.
    """

    def __init__(self, *args):
        if len(args) == 2:
            self['title'] = args[0]
            self['url'] = args[1]

    def __setitem__(self, key, value):
        if key == 'url':
            if not self.has_key('original_url'):
                self['original_url'] = value
        dict.__setitem__(self, key, value)
    
    def safe_str(self):
        return "%s | %s" % (self['title'], self['url'])

    def isvalid(self):
        """Return True if entry is valid. Return False if this cannot be used."""
        if not self.has_key('title'):
            return False
        if not self.has_key('url'):
            return True
        return True

class ModuleCache:

    """
        Provides dictionary-like persistent storage for modules, allows saving key value pair
        for n number of days. Purges old entries to keep storage size in reasonable sizes.
    """

    log = logging.getLogger('modulecache')

    def __init__(self, name, storage):
        self.__storage = storage.setdefault(name, {})
        self._cache = None
        self.__namespace = None

    def set_namespace(self, name):
        self._cache = self.__storage.setdefault(name, {})
        self.__namespace = name
        self.__purge()

    def get_namespace(self):
        return self.__namespace

    def get_namespaces(self):
        """Return array of known namespaces in this cache"""
        return self.__storage.keys()
    
    def store(self, key, value, days=45):
        """Stores key value pair for number of days. Non yaml compatible values are not saved."""
        item = {}
        item['stored'] = datetime.today().strftime('%Y-%m-%d')
        item['days'] = days
        item['value'] = value
        self._cache[key] = item

    def storedefault(self, key, value, days=45):
        """Identical to dictionary setdefault"""
        undefined = object()
        item = self.get(key, undefined)
        if item is undefined:
            self.log.debug('storing default for %s, value %s' % (key, value))
            self.store(key, value, days)
            return self.get(key)
        else:
            return item

    def get(self, key, default=None):
        """Return value by key from cache. Return None or default if not found"""
        item = self._cache.get(key)
        if item is None:
            return default
        else:
            # reading value should update "stored" date .. TODO: refactor stored -> access & days -> keep?
            item['stored'] = datetime.today().strftime('%Y-%m-%d')
            # HACK, for some reason there seems to be items without value, most likely elusive rare bug ..
            if not item.has_key('value'):
                self.log.warning('BUGFIX: Key %s is missing value, using default.' % key)
                return default
            return item['value']
            
    def remove(self, key):
        """Removes a key from cache"""
        return self._cache.pop(key)
        
    def has_key(self, key):
        return self._cache.has_key(key)

    def __purge(self):
        """Remove all values from cache that have passed their expiration date"""
        now = datetime.today()
        for key in self._cache.keys():
            item = self._cache[key]
            y, m, d = item['stored'].split('-')
            stored = datetime(int(y), int(m), int(d))
            delta = now - stored
            if delta.days > item['days']:
                self.log.debug('Purging from cache %s' % (str(item)))
                self._cache.pop(key)

class Feed:

    def __init__(self, manager, name, config):
        """
            Represents one feed in configuration.

            name - name of the feed
            config - yaml configuration (dict)
        """
        self.name = name
        self.config = config
        self.manager = manager

        self.cache = ModuleCache(name, manager.session.setdefault('cache', {}))
        self.shared_cache = ModuleCache('_shared_', manager.session.setdefault('cache', {}))

        self.entries = []
        
        # You should NOT change these arrays at any point, and in most cases not even read them!
        # Mainly public since unit test needs these
        
        self.accepted = [] # accepted entries are always accepted, filtering does not affect them
        self.filtered = [] # filtered entries
        self.rejected = [] # rejected entries are removed unconditionally, even if accepted
        self.failed = []

        # TODO: feed.abort() should be done by using exception, not a flag that has to be checked everywhere

        # flags and counters
        self.unittest = False
        self.__abort = False
        self.__purged = 0
        
        # state
        self.__current_event = None
        self.__current_module = None
        
    def _purge(self):
        """Purge filtered entries from feed. Call this from module only if you know what you're doing."""
        self.__purge(self.filtered, self.accepted)

    def __purge_failed(self):
        """Purge failed entries from feed."""
        self.__purge(self.failed, [], False)

    def __purge_rejected(self):
        """Purge rejected entries from feed."""
        self.__purge(self.rejected)

    def __purge(self, entries, not_in_list=[], count=True):
        """Purge entries in list from feed.entries"""
        for entry in self.entries[:]:
            if entry in entries and entry not in not_in_list:
                log.debug('Purging entry %s' % entry.safe_str())
                self.entries.remove(entry)
                if count:
                    self.__purged += 1

    def accept(self, entry, reason=None):
        """Accepts this entry."""
        if not entry in self.accepted:
            self.accepted.append(entry)
            if entry in self.filtered:
                self.filtered.remove(entry)
                self.verbose_details('Accepted previously filtered %s' % entry['title'])
            else:
                self.verbose_details('Accepted %s' % entry['title'], reason)

    def filter(self, entry, reason=None):
        """Mark entry to be filtered unless told otherwise. Entry may still be accepted."""
        # accepted checked only because it makes more sense when verbose details
        if not entry in self.filtered and not entry in self.accepted:
            self.filtered.append(entry)
            self.verbose_details('Filtered %s' % entry['title'], reason)

    def reject(self, entry, reason=None):
        """Reject this entry immediately and permanently."""
        # schedule immediately filtering after this module has done execution
        if not entry in self.rejected:
            self.rejected.append(entry)
            self.verbose_details('Rejected %s' % entry['title'], reason)

    def fail(self, entry, reason=None):
        """Mark entry as failed."""
        log.debug("Marking entry '%s' as failed" % entry['title'])
        if not entry in self.failed:
            self.failed.append(entry)
            self.manager.add_failed(entry)
            self.verbose_details('Failed %s' % entry['title'], reason)

    def abort(self, **kwargs):
        """Abort this feed execution, no more modules will be executed."""
        if not self.__abort and not kwargs.get('silent', False):
            log.info('Aborting feed %s' % self.name)
        self.__abort = True
        self.__run_event('abort')

    def get_input_url(self, keyword):
        # TODO: move to better place?
        """
            Helper method for modules. Return url for a specified keyword.
            Supports configuration in following forms:
                <keyword>: <address>
            and
                <keyword>:
                    url: <address>
        """
        if isinstance(self.config[keyword], dict):
            if not self.config[keyword].has_key('url'):
                raise Exception('Input %s has invalid configuration, url is missing.' % keyword)
            return self.config[keyword]['url']
        else:
            return self.config[keyword]

    def log_once(self, s, logger=log):
        """Log string s once"""
        import md5
        m = md5.new()
        m.update(s)
        md5sum = m.hexdigest()
        seen = self.shared_cache.get('log-%s' % md5sum, False)
        if (seen): return
        self.shared_cache.store('log-%s' % md5sum, True, 30)
        logger.info(s)

    # TODO: all these verbose methods are confusing
    def verbose_progress(self, s, logger=log):
        """Verbose progress, outputs only in non quiet mode."""
        # TODO: implement trough own logger?
        if not self.manager.options.quiet and not self.unittest:
            logger.info(s)
          
    def verbose_details(self, msg, reason):
        """Verbose if details option is enabled"""
        # TODO: implement trough own logger?
        if self.manager.options.details:
            try:
                reson_str = ''
                if reason:
                    reason_str = ' (%s)' % reason
                print "+ %-8s %-12s %s%s" % (self.__current_event, self.__current_module, msg, reason_str)
            except:
                print "+ %-8s %-12s ERROR: Unable to print %s" % (self.__current_event, self.__current_module, repr(msg))

    def verbose_details_entries(self):
        """If details option is enabled, print all produced entries"""
        if self.manager.options.details:
            for entry in self.entries:
                self.verbose_details('%s' % entry['title'])
            
    def __get_priority(self, module, event):
        """Return order for module in this feed. Uses default value if no value is configured."""
        priority = module.get('priorities', {}).get(event, 0)
        keyword = module['name']
        if self.config.has_key(keyword):
            if isinstance(self.config[keyword], dict):
                priority = self.config[keyword].get('priority', priority)
        return priority

    def __sort_modules(self, a, b, event):
        a = self.__get_priority(a, event)
        b = self.__get_priority(b, event)
        return cmp(a, b)

    def __set_namespace(self, name):
        """Switch namespace in session"""
        self.cache.set_namespace(name)
        self.shared_cache.set_namespace(name)
        
    def __run_event(self, event):
        """Execute module events if module is configured for this feed."""
        modules = self.manager.get_modules_by_event(event)
        # Sort modules based on module event priority
        # Priority can be also configured in which case given value overwrites module default.
        modules.sort(lambda x,y: self.__sort_modules(x,y, event))
        modules.reverse()

        for module in modules:
            keyword = module['name']
            if self.config.has_key(keyword) or module['builtin']:
                # set cache namespaces to this module realm
                self.__set_namespace(keyword)
                # store execute info
                self.__current_event = event
                self.__current_module = keyword
                # call the module
                try:
                    method = self.manager.event_methods[event]
                    getattr(module['instance'], method)(self)
                except ModuleWarning, m:
                    # this warning should be logged only once (may keep repeating)
                    if m.kwargs.get('log_once', False):
                        self.log_once(m, m.log)
                    else:
                        m.log.warning(m)
                except Exception, e:
                    log.exception('Unhandled error in module %s: %s' % (keyword, e))
                    self.abort()
                # remove entries
                self.__purge_rejected()
                self.__purge_failed()
                # check for priority operations
                if self.__abort: return
    
    def execute(self):
        """Execute this feed, runs events in order of events array."""
        # validate configuration
        errors = self.validate()
        if self.__abort: return
        if self.manager.options.validate:
            if not errors:
                print 'Feed \'%s\' passed' % self.name
                return
            
        # run events
        for event in self.manager.events:
            # when learning, skip few events
            if self.manager.options.learn:
                if event in ['download', 'output']: 
                    # log keywords not executed
                    modules = self.manager.get_modules_by_event(event)
                    for module in modules:
                        if self.config.has_key(module['name']):
                            log.info('Feed %s keyword %s is not executed because of learn/reset.' % (self.name, module['name']))
                    continue
            # run all modules with this event
            self.__run_event(event)
            # purge filtered entries between events
            # rejected and failed entries are purged between modules
            self._purge()
            # verbose some progress
            if event == 'input':
                self.verbose_details_entries()
                if not self.entries:
                    self.verbose_progress('Feed %s didn\'t produce any entries. This is likely to be miss configured or non-functional input.' % self.name)
                else:
                    self.verbose_progress('Feed %s produced %s entries.' % (self.name, len(self.entries)))
            if event == 'filter':
                self.verbose_progress('Feed %s filtered %s entries (%s remains).' % (self.name, self.__purged, len(self.entries)))
            # if abort flag has been set feed should be aborted now
            if self.__abort:
                return

    def terminate(self):
        """Execute terminate event for this feed"""
        if self.__abort: return
        self.__run_event('terminate')

    def validate(self):
        """Module configuration validation. Return array of error messages that were detected."""
        validate_errors = []
        # validate all modules
        for keyword in self.config:
            module = self.manager.modules.get(keyword)
            if not module:
                validate_errors.append('Unknown keyword \'%s\'' % keyword)
                continue
            if hasattr(module['instance'], 'validator'):
                try:
                    validator = module['instance'].validator()
                except TypeError:
                    log.critical('invalid validator method in module %s' % keyword)
                    continue
                errors = validator.validate(self.config[keyword])
                if errors:
                    for msg in validator.errors.messages:
                        validate_errors.append('%s %s' % (keyword, msg))
            else:
                log.warning('Used module %s does not support validating. Please notify author!' % keyword)
                
        # log errors and abort
        if validate_errors:
            log.critical('Feed \'%s\' has configuration errors:' % self.name)
            for error in validate_errors:
                log.error(error)
            # feed has errors, abort it
            self.abort()
                
        return validate_errors