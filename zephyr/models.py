from django.db import models
from django.conf import settings
from django.contrib.auth.models import User
import hashlib
import base64
from zephyr.lib.cache import cache_with_key
from zephyr.lib.initial_password import initial_password, initial_api_key
import os
import simplejson
from django.db import transaction, IntegrityError
from zephyr.lib import bugdown
from zephyr.lib.avatar import gravatar_hash
from zephyr.lib.context_managers import lockfile
import requests
from django.contrib.auth.models import UserManager
from django.utils import timezone
from django.contrib.sessions.models import Session
import time
import subprocess
import traceback
import re
from django.utils.html import escape
from zephyr.lib.time import datetime_to_timestamp

MAX_SUBJECT_LENGTH = 60
MAX_MESSAGE_LENGTH = 10000

@cache_with_key(lambda self: 'display_recipient_dict:%d' % (self.id,))
def get_display_recipient(recipient):
    """
    recipient: an instance of Recipient.

    returns: an appropriate object describing the recipient.  For a
    stream this will be the stream name as a string.  For a huddle or
    personal, it will be an array of dicts about each recipient.
    """
    if recipient.type == Recipient.STREAM:
        stream = Stream.objects.get(id=recipient.type_id)
        return stream.name

    # We don't really care what the ordering is, just that it's deterministic.
    user_profile_list = (UserProfile.objects.filter(subscription__recipient=recipient)
                                            .select_related()
                                            .order_by('user__email'))
    return [{'email': user_profile.user.email,
             'full_name': user_profile.full_name,
             'short_name': user_profile.short_name} for user_profile in user_profile_list]

class Realm(models.Model):
    domain = models.CharField(max_length=40, db_index=True, unique=True)

    def __repr__(self):
        return "<Realm: %s %s>" % (self.domain, self.id)
    def __str__(self):
        return self.__repr__()

class UserProfile(models.Model):
    user = models.OneToOneField(User)
    full_name = models.CharField(max_length=100)
    short_name = models.CharField(max_length=100)
    pointer = models.IntegerField()
    last_pointer_updater = models.CharField(max_length=64)
    realm = models.ForeignKey(Realm)
    api_key = models.CharField(max_length=32)
    enable_desktop_notifications = models.BooleanField(default=True)

    def __repr__(self):
        return "<UserProfile: %s %s>" % (self.user.email, self.realm)
    def __str__(self):
        return self.__repr__()

    @classmethod
    def create(cls, user, realm, full_name, short_name):
        """When creating a new user, make a profile for him or her."""
        if not cls.objects.filter(user=user):
            profile = cls(user=user, pointer=-1, realm=realm,
                          full_name=full_name, short_name=short_name)
            profile.api_key = initial_api_key(user.email)
            profile.save()
            # Auto-sub to the ability to receive personals.
            recipient = Recipient.objects.create(type_id=profile.id, type=Recipient.PERSONAL)
            Subscription.objects.create(user_profile=profile, recipient=recipient)
            return profile

class PreregistrationUser(models.Model):
    email = models.EmailField(unique=True)
    # status: whether an object has been confirmed.
    #   if confirmed, set to confirmation.settings.STATUS_ACTIVE
    status = models.IntegerField(default=0)

class MitUser(models.Model):
    email = models.EmailField(unique=True)
    # status: whether an object has been confirmed.
    #   if confirmed, set to confirmation.settings.STATUS_ACTIVE
    status = models.IntegerField(default=0)

# create_user_hack is the same as Django's User.objects.create_user,
# except that we don't save to the database so it can used in
# bulk_creates
def create_user_hack(username, password, email, active):
    now = timezone.now()
    email = UserManager.normalize_email(email)
    user = User(username=username, email=email,
                is_staff=False, is_active=active, is_superuser=False,
                last_login=now, date_joined=now)

    if active:
        user.set_password(password)
    else:
        user.set_unusable_password()
    return user

def set_default_streams(realm, stream_names):
    DefaultStream.objects.filter(realm=realm).delete()
    for stream_name in stream_names:
        stream = create_stream_if_needed(realm, stream_name)
        DefaultStream.objects.create(stream=stream, realm=realm)

def add_default_subs(user_profile):
    for default in DefaultStream.objects.filter(realm=user_profile.realm):
        do_add_subscription(user_profile, default.stream)

def create_user_base(email, password, active=True):
    # NB: the result of Base32 + truncation is not a valid Base32 encoding.
    # It's just a unique alphanumeric string.
    # Use base32 instead of base64 so we don't have to worry about mixed case.
    # Django imposes a limit of 30 characters on usernames.
    email_hash = hashlib.sha256(settings.HASH_SALT + email).digest()
    username = base64.b32encode(email_hash)[:30]
    return create_user_hack(username, password, email, active)

def create_user(email, password, realm, full_name, short_name,
                active=True):
    user = create_user_base(email=email, password=password,
                            active=active)
    user.save()
    return UserProfile.create(user, realm, full_name, short_name)

def do_create_user(email, password, realm, full_name, short_name,
                   active=True):
    log_event({'type': 'user_created',
               'timestamp': time.time(),
               'full_name': full_name,
               'short_name': short_name,
               'user': email})
    return create_user(email, password, realm, full_name, short_name, active)

def compute_mit_user_fullname(email):
    try:
        # Input is either e.g. starnine@mit.edu or user|CROSSREALM.INVALID@mit.edu
        match_user = re.match(r'^([a-zA-Z0-9_.-]+)(\|.+)?@mit\.edu$', email.lower())
        if match_user and match_user.group(2) is None:
            dns_query = "%s.passwd.ns.athena.mit.edu" % (match_user.group(1),)
            proc = subprocess.Popen(['host', '-t', 'TXT', dns_query],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            out, _err_unused = proc.communicate()
            if proc.returncode == 0:
                # Parse e.g. 'starnine:*:84233:101:Athena Consulting Exchange User,,,:/mit/starnine:/bin/bash'
                # for the 4th passwd entry field, aka the person's name.
                hesiod_name = out.split(':')[4].split(',')[0].strip()
                if hesiod_name == "":
                    return email
                return hesiod_name
        elif match_user:
            return match_user.group(1).lower() + "@" + match_user.group(2).upper()[1:]
    except:
        print ("Error getting fullname for %s:" % (email,))
        traceback.print_exc()
    return email.lower()

def create_mit_user_if_needed(realm, email):
    try:
        return UserProfile.objects.get(user__email=email)
    except UserProfile.DoesNotExist:
        try:
            # Forge a user for this person
            return create_user(email, initial_password(email), realm,
                               compute_mit_user_fullname(email), email.split("@")[0],
                               active=False)
        except IntegrityError:
            # Unless we raced with another thread doing the same
            # thing, in which case we should get the user they made
            transaction.commit()
            return UserProfile.objects.get(user__email=email)

def create_stream_if_needed(realm, stream_name):
    (stream, created) = Stream.objects.get_or_create(
        realm=realm, name__iexact=stream_name,
        defaults={'name': stream_name})
    if created:
        Recipient.objects.create(type_id=stream.id, type=Recipient.STREAM)
    return stream

class Stream(models.Model):
    name = models.CharField(max_length=30, db_index=True)
    realm = models.ForeignKey(Realm, db_index=True)

    def __repr__(self):
        return "<Stream: %s>" % (self.name,)
    def __str__(self):
        return self.__repr__()

    class Meta:
        unique_together = ("name", "realm")

    @classmethod
    def create(cls, name, realm):
        stream = cls(name=name, realm=realm)
        stream.save()

        recipient = Recipient.objects.create(type_id=stream.id,
                                             type=Recipient.STREAM)
        return (stream, recipient)

class Recipient(models.Model):
    type_id = models.IntegerField(db_index=True)
    type = models.PositiveSmallIntegerField(db_index=True)
    # Valid types are {personal, stream, huddle}
    PERSONAL = 1
    STREAM = 2
    HUDDLE = 3

    class Meta:
        unique_together = ("type", "type_id")

    # N.B. If we used Django's choice=... we would get this for free (kinda)
    _type_names = {
        PERSONAL: 'personal',
        STREAM:   'stream',
        HUDDLE:   'huddle' }

    def type_name(self):
        # Raises KeyError if invalid
        return self._type_names[self.type]

    def __repr__(self):
        display_recipient = get_display_recipient(self)
        return "<Recipient: %s (%d, %s)>" % (display_recipient, self.type_id, self.type)

class Client(models.Model):
    name = models.CharField(max_length=30, db_index=True, unique=True)

@transaction.commit_on_success
def get_client(name):
    try:
        (client, _) = Client.objects.get_or_create(name=name)
    except IntegrityError:
        # If we're racing with other threads trying to create this
        # client, get_or_create will throw IntegrityError (because our
        # database is enforcing the no-duplicate-objects constraint);
        # in this case one should just re-fetch the object.  This race
        # actually happens with populate_db.
        #
        # Much of the rest of our code that writes to the database
        # doesn't handle this duplicate object on race issue correctly :(
        transaction.commit()
        return Client.objects.get(name=name)
    return client

def linebreak(string):
    return string.replace('\n\n', '<p/>').replace('\n', '<br/>')

class Message(models.Model):
    sender = models.ForeignKey(UserProfile)
    recipient = models.ForeignKey(Recipient)
    subject = models.CharField(max_length=MAX_SUBJECT_LENGTH, db_index=True)
    content = models.TextField()
    pub_date = models.DateTimeField('date published', db_index=True)
    sending_client = models.ForeignKey(Client)

    def __repr__(self):
        display_recipient = get_display_recipient(self.recipient)
        return "<Message: %s / %s / %r>" % (display_recipient, self.subject, self.sender)
    def __str__(self):
        return self.__repr__()

    @cache_with_key(lambda self, apply_markdown: 'message_dict:%d:%d' % (self.id, apply_markdown))
    def to_dict(self, apply_markdown):
        # Messages arrive in the Tornado process with the dicts already rendered.
        # This avoids running the Markdown parser and some database queries in the single-threaded
        # Tornado server.
        #
        # This field is not persisted to the database and will disappear if the object is re-fetched.
        if hasattr(self, 'precomputed_dicts'):
            return self.precomputed_dicts['text/html' if apply_markdown else 'text/x-markdown']

        display_recipient = get_display_recipient(self.recipient)
        if self.recipient.type == Recipient.STREAM:
            display_type = "stream"
        elif self.recipient.type in (Recipient.HUDDLE, Recipient.PERSONAL):
            display_type = "private"
            if len(display_recipient) == 1:
                # add the sender in if this isn't a message between
                # someone and his self, preserving ordering
                recip = {'email': self.sender.user.email,
                         'full_name': self.sender.full_name,
                         'short_name': self.sender.short_name};
                if recip['email'] < display_recipient[0]['email']:
                    display_recipient = [recip, display_recipient[0]]
                elif recip['email'] > display_recipient[0]['email']:
                    display_recipient = [display_recipient[0], recip]
        else:
            display_type = self.recipient.type_name()

        obj = dict(
            id                = self.id,
            sender_email      = self.sender.user.email,
            sender_full_name  = self.sender.full_name,
            sender_short_name = self.sender.short_name,
            type              = display_type,
            display_recipient = display_recipient,
            recipient_id      = self.recipient.id,
            subject           = self.subject,
            timestamp         = datetime_to_timestamp(self.pub_date),
            gravatar_hash     = gravatar_hash(self.sender.user.email))

        if apply_markdown:
            if (self.sender.realm.domain != "customer1.invalid" or
                self.sending_client.name in ('API', 'github_bot')):
                obj['content'] = bugdown.convert(self.content)
            else:
                obj['content'] = linebreak(escape(self.content))
            obj['content_type'] = 'text/html'
        else:
            obj['content'] = self.content
            obj['content_type'] = 'text/x-markdown'

        return obj

    def to_log_dict(self):
        return dict(
            id                = self.id,
            sender_email      = self.sender.user.email,
            sender_full_name  = self.sender.full_name,
            sender_short_name = self.sender.short_name,
            sending_client    = self.sending_client.name,
            type              = self.recipient.type_name(),
            recipient         = get_display_recipient(self.recipient),
            subject           = self.subject,
            content           = self.content,
            timestamp         = datetime_to_timestamp(self.pub_date))

    @classmethod
    def remove_unreachable(cls):
        """Remove all Messages that are not referred to by any UserMessage."""
        cls.objects.exclude(id__in = UserMessage.objects.values('message_id')).delete()

class UserMessage(models.Model):
    user_profile = models.ForeignKey(UserProfile)
    message = models.ForeignKey(Message)
    # We're not using the archived field for now, but create it anyway
    # since this table will be an unpleasant one to do schema changes
    # on later
    archived = models.BooleanField()

    class Meta:
        unique_together = ("user_profile", "message")

    def __repr__(self):
        display_recipient = get_display_recipient(self.message.recipient)
        return "<UserMessage: %s / %s>" % (display_recipient, self.user_profile.user.email)

user_hash = {}
def get_user_profile_by_id(uid):
    if uid in user_hash:
        return user_hash[uid]
    return UserProfile.objects.select_related().get(id=uid)

# Store an event in the log for re-importing messages
def log_event(event):
    if "timestamp" not in event:
        event["timestamp"] = time.time()
    with lockfile(settings.MESSAGE_LOG + '.lock'):
        with open(settings.MESSAGE_LOG, 'a') as log:
            log.write(simplejson.dumps(event) + '\n')

def log_message(message):
    if not message.sending_client.name.startswith("test:"):
        log_event(message.to_log_dict())

def do_send_message(message, no_log=False):
    # Log the message to our message log for populate_db to refill
    if not no_log:
        log_message(message)

    if message.recipient.type == Recipient.PERSONAL:
        recipients = list(set([get_user_profile_by_id(message.recipient.type_id),
                               get_user_profile_by_id(message.sender_id)]))
        # For personals, you send out either 1 or 2 copies of the message, for
        # personals to yourself or to someone else, respectively.
        assert((len(recipients) == 1) or (len(recipients) == 2))
    elif (message.recipient.type == Recipient.STREAM or
          message.recipient.type == Recipient.HUDDLE):
        recipients = [s.user_profile for
                      s in Subscription.objects.select_related().filter(recipient=message.recipient, active=True)]
    else:
        raise ValueError('Bad recipient type')

    # Save the message receipts in the database
    # TODO: Use bulk_create here
    with transaction.commit_on_success():
        message.save()
        for user_profile in recipients:
            # Only deliver messages to "active" user accounts
            if user_profile.user.is_active:
                UserMessage(user_profile=user_profile, message=message).save()

    # We can only publish messages to longpolling clients if the Tornado server is running.
    if settings.TORNADO_SERVER:
        # Render Markdown etc. here, so that the single-threaded Tornado server doesn't have to.
        # TODO: Reduce duplication in what we send.
        rendered = { 'text/html':       message.to_dict(apply_markdown=True),
                     'text/x-markdown': message.to_dict(apply_markdown=False) }
        requests.post(settings.TORNADO_SERVER + '/notify_new_message', data=dict(
            secret   = settings.SHARED_SECRET,
            message  = message.id,
            rendered = simplejson.dumps(rendered),
            users    = simplejson.dumps([str(user.id) for user in recipients])))

def internal_send_message(sender_email, recipient_type, recipient_name,
                          subject, content):
    if len(content) > MAX_MESSAGE_LENGTH:
        content = content[0:3900] + "\n\n[message was too long and has been truncated]"
    message = Message()
    message.sender = UserProfile.objects.get(user__email=sender_email)
    message.recipient = Recipient.objects.get(type_id=create_stream_if_needed(
        message.sender.realm, recipient_name).id, type=recipient_type)
    message.subject = subject
    message.content = content
    message.pub_date = timezone.now()
    message.sending_client = get_client("Internal")

    do_send_message(message)

class Subscription(models.Model):
    user_profile = models.ForeignKey(UserProfile)
    recipient = models.ForeignKey(Recipient)
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("user_profile", "recipient")

    def __repr__(self):
        return "<Subscription: %r -> %r>" % (self.user_profile, self.recipient)
    def __str__(self):
        return self.__repr__()

def do_add_subscription(user_profile, stream, no_log=False):
    recipient = Recipient.objects.get(type_id=stream.id,
                                      type=Recipient.STREAM)
    (subscription, created) = Subscription.objects.get_or_create(
        user_profile=user_profile, recipient=recipient,
        defaults={'active': True})
    did_subscribe = created
    if not subscription.active:
        did_subscribe = True
        subscription.active = True
        subscription.save()
    if did_subscribe and not no_log:
        log_event({'type': 'subscription_added',
                   'user': user_profile.user.email,
                   'name': stream.name,
                   'domain': stream.realm.domain})
    return did_subscribe

def do_remove_subscription(user_profile, stream, no_log=False):
    recipient = Recipient.objects.get(type_id=stream.id,
                                      type=Recipient.STREAM)
    maybe_sub = Subscription.objects.filter(user_profile=user_profile,
                                    recipient=recipient)
    if len(maybe_sub) == 0:
        return False
    subscription = maybe_sub[0]
    did_remove = subscription.active
    subscription.active = False
    subscription.save()
    if did_remove and not no_log:
        log_event({'type': 'subscription_removed',
                   'user': user_profile.user.email,
                   'name': stream.name,
                   'domain': stream.realm.domain})
    return did_remove

def log_subscription_property_change(user_email, property, property_dict):
    event = {'type': 'subscription_property',
             'property': property,
             'user': user_email}
    event.update(property_dict)
    log_event(event)

def do_activate_user(user, log=True, join_date=timezone.now()):
    user.is_active = True
    user.set_password(initial_password(user.email))
    user.date_joined = join_date
    user.save()
    if log:
        log_event({'type': 'user_activated',
                   'user': user.email})

def do_change_password(user, password, log=True, commit=True):
    user.set_password(password)
    if commit:
        user.save()
    if log:
        log_event({'type': 'user_change_password',
                   'user': user.email,
                   'pwhash': user.password})

def do_change_full_name(user_profile, full_name, log=True):
    user_profile.full_name = full_name
    user_profile.save()
    if log:
        log_event({'type': 'user_change_full_name',
                   'user': user_profile.user.email,
                   'full_name': full_name})

def do_create_realm(domain, replay=False):
    realm, created = Realm.objects.get_or_create(domain=domain)
    if created and not replay:
        # Log the event
        log_event({"type": "realm_created",
                   "domain": domain})

        # Sent a notification message
        message = Message()
        message.sender = UserProfile.objects.get(user__email="humbug+signups@humbughq.com")
        message.recipient = Recipient.objects.get(type_id=create_stream_if_needed(
                message.sender.realm, "signups").id, type=Recipient.STREAM)
        message.subject = domain
        message.content = "Signups enabled."
        message.pub_date = timezone.now()
        message.sending_client = get_client("Internal")

        do_send_message(message)
    return (realm, created)

def do_change_enable_desktop_notifications(user_profile, enable_desktop_notifications, log=True):
    user_profile.enable_desktop_notifications = enable_desktop_notifications
    user_profile.save()
    if log:
        log_event({'type': 'enable_desktop_notifications_changed',
                   'user': user_profile.user.email,
                   'enable_desktop_notifications': enable_desktop_notifications})

class Huddle(models.Model):
    # TODO: We should consider whether using
    # CommaSeparatedIntegerField would be better.
    huddle_hash = models.CharField(max_length=40, db_index=True, unique=True)

def get_huddle_hash(id_list):
    id_list = sorted(set(id_list))
    hash_key = ",".join(str(x) for x in id_list)
    return hashlib.sha1(hash_key).hexdigest()

def get_huddle(id_list):
    huddle_hash = get_huddle_hash(id_list)
    (huddle, created) = Huddle.objects.get_or_create(huddle_hash=huddle_hash)
    if created:
        recipient = Recipient.objects.create(type_id=huddle.id,
                                             type=Recipient.HUDDLE)
        # Add subscriptions
        for uid in id_list:
            Subscription.objects.create(recipient = recipient,
                                        user_profile = UserProfile.objects.get(id=uid))
    return huddle

# This function is used only by tests.
# We have faster implementations within the app itself.
def filter_by_subscriptions(messages, user):
    user_profile = UserProfile.objects.get(user=user)
    user_messages = []
    subscriptions = [sub.recipient for sub in
                     Subscription.objects.filter(user_profile=user_profile, active=True)]
    for message in messages:
        # If you are subscribed to the personal or stream, or if you
        # sent the personal, you can see the message.
        if (message.recipient in subscriptions) or \
                (message.recipient.type == Recipient.PERSONAL and
                 message.sender == user_profile):
            user_messages.append(message)

    return user_messages

def clear_database():
    for model in [Message, Stream, UserProfile, User, Recipient,
                  Realm, Subscription, Huddle, UserMessage, Client,
                  DefaultStream]:
        model.objects.all().delete()
    Session.objects.all().delete()

class UserActivity(models.Model):
    user_profile = models.ForeignKey(UserProfile)
    client = models.ForeignKey(Client)
    query = models.CharField(max_length=50, db_index=True)

    count = models.IntegerField()
    last_visit = models.DateTimeField('last visit')

    class Meta:
        unique_together = ("user_profile", "client", "query")

class DefaultStream(models.Model):
    realm = models.ForeignKey(Realm)
    stream = models.ForeignKey(Stream)

    class Meta:
        unique_together = ("realm", "stream")

# FIXME: The foreign key relationship here is backwards.
#
# We can't easily get a list of streams and their associated colors (if any) in
# a single query.  See zephyr.views.gather_subscriptions for an example.
#
# We should change things around so that is possible.  Probably this should
# just be a column on Subscription.
class StreamColor(models.Model):
    subscription = models.ForeignKey(Subscription)
    color = models.CharField(max_length=10)
