import re
from xml.parsers.expat import ParserCreate
from time import gmtime
from datetime import datetime, time, timedelta
from sms import extractsms

import hashlib

try:
    from urllib2 import build_opener,install_opener, \
        HTTPCookieProcessor,Request,urlopen
    from urllib import urlencode,quote
except ImportError:
    from urllib.request import build_opener,install_opener, \
        HTTPCookieProcessor,Request,urlopen
    from urllib.parse import urlencode,quote

try:
    from http.cookiejar import LWPCookieJar as CookieJar
except ImportError:
    from cookielib import LWPCookieJar as CookieJar

try:
    from json import loads
except ImportError:
    from django.utils.simplejson import loads

#try:
#    input = raw_input
#except NameError:
#    input = input

sha1_re = re.compile(r'^[a-fA-F0-9]{40}$')

# Removed due to appengine
#def print_(*values, **kwargs):
#    """
#    Implementation of Python3's print function
#    
#    Prints the values to a stream, or to sys.stdout by default.
#    Optional keyword arguments:
#    
#    file: a file-like object (stream); defaults to the current sys.stdout.
#    sep:  string inserted between values, default a space.
#    end:  string appended after the last value, default a newline.
#    """
#    fo = kwargs.pop('file', stdout)
#    fo.write(kwargs.pop('sep', ' ').join(map(str, values)))
#    fo.write(kwargs.pop('end', '\n'))
#    fo.flush()

def is_sha1(s):
    """
    Returns ``True`` if the string is a SHA1 hash
    """
    return bool(sha1_re.match(s))

def validate_response(response):
    """
    Validates that the JSON response is A-OK
    """
    try:
        assert 'ok' in response and response['ok']
    except AssertionError:
        raise ValidationError('There was a problem with GV: %s' % response)

def load_and_validate(response):
    """
    Loads JSON data from http response then validates
    """
    validate_response(loads(response.content))

class ValidationError(Exception):
    """
    Bombs when response code back from Voice 500s
    """

class LoginError(Exception):
    """
    Occurs when login credentials are incorrect
    """
    
class ParsingError(Exception):
    """
    Happens when XML feed parsing fails
    """
    
class JSONError(Exception):
    """
    Failed JSON deserialization
    """
    
class DownloadError(Exception):
    """
    Cannot download message, probably not in voicemail/recorded
    """
    
class ForwardingError(Exception):
    """
    Forwarding number given was incorrect
    """
    
class NoCredentialsError(Exception):
    """
    Email or Password were not specified in the config or passed
    as arguments to Voice.login
    """

class NoSuchFeedError(Exception):
    """
    The requested feed for pagination does not exist
    """
    
class AttrDict(dict):
    def __getattr__(self, attr):
        if attr in self:
            return self[attr]

class Phone(AttrDict):
    """
    Wrapper for phone objects used for phone specific methods
    Attributes are:
    
     * id: int
     * phoneNumber: i18n phone number
     * formattedNumber: humanized phone number string
     * we: data dict
     * wd: data dict
     * verified: bool
     * name: strign label
     * smsEnabled: bool
     * scheduleSet: bool
     * policyBitmask: int
     * weekdayTimes: list
     * dEPRECATEDDisabled: bool
     * weekdayAllDay: bool
     * telephonyVerified
     * weekendTimes: list
     * active: bool
     * weekendAllDay: bool
     * enabledForOthers: bool
     * type: int (1 - Home, 2 - Mobile, 3 - Work, 4 - Gizmo)
            
    """
    def __init__(self, voice, data):
        self.voice = voice
        super(Phone, self).__init__(data)
    
    def enable(self,):
        """
        Enables this phone for usage
        """
        return self.__call_forwarding()

    def disable(self):
        """
        Disables this phone
        """
        return self.__call_forwarding('0')
        
    def __call_forwarding(self, enabled='1'):
        """
        Enables or disables this phone
        """
        self.voice.__validate_special_page('default_forward',
            {'enabled':enabled, 'phoneId': self.id})
        
    def __str__(self):
        return self.phoneNumber
    
    def __repr__(self):
        return '<Phone %s>' % self.phoneNumber
        
class Message(AttrDict):
    """
    Wrapper for all call/sms message instances stored in Google Voice
    Attributes are:
    
     * id: SHA1 identifier
     * isTrash: bool
     * displayStartDateTime: datetime
     * star: bool
     * isSpam: bool
     * startTime: gmtime
     * labels: list
     * displayStartTime: time
     * children: str
     * note: str
     * isRead: bool
     * displayNumber: str
     * relativeStartTime: str
     * phoneNumber: str
     * type: int
     
    """
    def __init__(self, folder, id, data):
        assert is_sha1(id), 'Message id not a SHA1 hash'
        self.folder = folder
        self.id = id
        super(AttrDict, self).__init__(data)
        self[u'epochTime'] = self['startTime']
        self['startTime'] = gmtime(int(self['startTime'])/1000)
        self['displayStartDateTime'] = datetime.strptime(
                self['displayStartDateTime'], '%m/%d/%y %I:%M %p')
        self['displayStartTime'] = self['displayStartDateTime'].time()
    
    def delete(self, trash=1):
        """
        Moves this message to the Trash. Use ``message.delete(0)`` to move it out of the Trash.
        """
        self.folder.voice.__messages_post('delete', self.id, trash=trash)

    def star(self, star=1):
        """
        Star this message. Use ``message.star(0)`` to unstar it.
        """
        self.folder.voice.__messages_post('star', self.id, star=star)
        
    def mark(self, read=1):
        """
        Mark this message as read. Use ``message.mark(0)`` to mark it as unread.
        """
        self.folder.voice.__messages_post('mark', self.id, read=read)
        
    def download(self, adir=None):
        """
        Download the message MP3 (if any). 
        Saves files to ``adir`` (defaults to current directory). 
        Message hashes can be found in ``self.voicemail().messages`` for example. 
        Returns location of saved file.        
        """
        return self.folder.voice.download(self, adir)

    def __str__(self):
        return self.id
    
    def __repr__(self):
        return '<Message #%s (%s)>' % (self.id, self.phoneNumber)

class SmsMessage(Message):
    """
    Wrapper for all parsed/flattened SMS messages in Google Voice
    Attributes are (in addition to those in Message):

     * smsId: SHA-1 hash
     * receivedDateTime: the computed time that the SMS was received
    """
    def __init__(self, folder, id, data):
        super(SmsMessage, self).__init__(folder, id, data)
        
        # Create the actual time the SMS was received.
        latest_date = self['displayStartDateTime'].date()
        received_time = datetime.strptime(self['time'], "%I:%M %p").time()
        received = datetime.combine(latest_date, received_time)
        if (received > self['displayStartDateTime']):
            received = received - timedelta(1)
        self['receivedDateTime'] = received

    def __cmp__(self, other):
        """
        SmsMessages are compared by their receivedDateTime and smsId if
        those match.
        """
        retval = cmp(self['receivedDateTime'], other['receivedDateTime'])
        if retval == 0:
            return cmp(self['smsId'], other['smsId'])
        return retval

    def __repr__(self):
        return '<SmsMessage #%s (%s)>' % (self.id, self.phoneNumber)    

class Folder(AttrDict):
    """
    Folder wrapper for feeds from Google Voice
    Attributes are:
    
     * totalSize: int (aka ``__len__``)
     * unreadCounts: dict
     * resultsPerPage: int
     * messages: list of Message instances
    """
    def __init__(self, voice, name, data):
        self.voice = voice
        self.name = name
        super(AttrDict, self).__init__(data)

        self._flattened = False
        
    def flattensms(self):
        """
        Parses the HTML output for SMS messages and flattens
        the folder such that each sms message is it's own message.

        Generates a new (hopefully) unique SHA-1 hash for the
        sms message.
        """
        if self._flattened == True:
            return

        smsmsgs = extractsms(getattr(self.voice, self.name).html)
        for sms in smsmsgs:
            h = hashlib.sha1()
            h.update("%s_[%s]" % (str(sms), sms['id']))
            new_id = h.hexdigest()
            
            sms['smsId'] = new_id
            self['messages'][new_id] = sms
            self['messages'][new_id].update(self['messages'][sms['id']])

        for sms in smsmsgs:
            if sms['id'] in self['messages']:
                del self['messages'][sms['id']]
        self._flattened = True

    def smsmessages(self):
        """
        Flatten the SMS conversations in this Folder and only return them
        """
        self.flattensms()
        msgs = []
        for msg in self['messages'].items():
            if 'smsId' in msg[1]:
                msgs.append(SmsMessage(self, msg[1]['id'], msg[1]))
        return msgs
    smsmessages = property(smsmessages)

    def messages(self):
        """
        Returns a list of all messages in this folder.
        """
        msgs = []
        for msg in self['messages'].items():
            if 'smsId' not in msg[1]:
                msgs.append(Message(self, *msg))
            else:
                msgs.append(SmsMessage(self, msg[1]['id'], msg[1]))
        return msgs
    messages = property(messages)
    
    def __len__(self):
        return self['totalSize']

    def __repr__(self):
        return '<Folder %s (%s)>' % (self.name, len(self))
    
class XMLParser(object):
    """
    XML Parser helper that can dig json and html out of the feeds. 
    The parser takes a ``Voice`` instance, page name, and function to grab data from. 
    Calling the parser calls the data function once, sets up the ``json`` and ``html``
    attributes and returns a ``Folder`` instance for the given page::
    
        >>> o = XMLParser(voice, 'voicemail', lambda: 'some xml payload')
        >>> o()
        ... <Folder ...>
        >>> o.json
        ... 'some json payload'
        >>> o.data
        ... 'loaded json payload'
        >>> o.html
        ... 'some html payload'
        
    """
    attr = None
        
    def start_element(self, name, attrs):
        if name in ('json','html'):
            self.attr = name
    def end_element(self, name): self.attr = None
    def char_data(self, data):
        if self.attr and data:
            setattr(self, self.attr, getattr(self, self.attr) + data)

    def __init__(self, voice, name, datafunc):
        self.json, self.html = '',''
        self.datafunc = datafunc
        self.voice = voice
        self.name = name
        
    def __call__(self):
        self.json, self.html = '',''
        parser = ParserCreate()
        parser.StartElementHandler = self.start_element
        parser.EndElementHandler = self.end_element
        parser.CharacterDataHandler = self.char_data
        try:
            data = self.datafunc()
            parser.Parse(data, 1)
        except:
            raise ParsingError
        return self.folder

    def folder(self):
        """
        Returns associated ``Folder`` instance for given page (``self.name``)
        """
        return Folder(self.voice, self.name, self.data)        
    folder = property(folder)
    
    def data(self):
        """
        Returns the parsed json information after calling the XMLParser
        """
        try:
            return loads(self.json)
        except:
            raise JSONError
    data = property(data)
    
