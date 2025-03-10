import asyncio
import inspect
import logging
import os
import sys
from panoramisk import Manager
import statsd

# Pometheus push gateway
STATSD_HOST = os.environ.get('STATSD_HOST', 'localhost:9125')
AMI_HOST = os.environ.get('AMI_HOST', 'localhost')
AMI_PORT = os.environ.get('AMI_PORT', '5038')
AMI_USER = os.environ.get('AMI_USER', 'asterisk')
AMI_SECRET = os.environ.get('AMI_SECRET', 'secret')

stats = statsd.StatsClient(*STATSD_HOST.split(':'))

loop = asyncio.get_event_loop()

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(level=logging.DEBUG)

# Asterisk AMI manager client
manager = Manager(loop=loop,
                  host=AMI_HOST, port=AMI_PORT,
                  username=AMI_USER,
                  secret=AMI_SECRET,
                  ping_interval=10,  # Periodically ping AMI (dead or alive)
                  reconnect_timeout=2,  # Timeout reconnect if connection lost
                  )
manager.loop.set_debug(True)

channels_current = {}  # Current channels gauge
sip_reachable_peers = set()
iax_reachable_peers = set()
sip_total_peers = 0
iax_total_peers = 0


def main():
    logger.info('Connecting to {}:{}.'.format(AMI_HOST, AMI_PORT))

    manager.connect()
    try:
        # loop.run_until_complete(ping(manager))
        loop.run_forever()
    except KeyboardInterrupt:
        loop.close()


@manager.register_event('FullyBooted')
def on_asterisk_FullyBooted(manager, msg):
    if msg.Uptime:
        stats.gauge('asterisk_uptime', int(msg.Uptime))
    if msg.LastReload:
        stats.gauge('asterisk_last_reload', int(msg.LastReload))
    # Get initial channels
    ShowChannels = yield from manager.send_action({'Action': 'CoreShowChannels'})
    channels = list(filter(lambda x: x.Event ==
                           'CoreShowChannel', ShowChannels))

    sip_channels = len(
        list(filter(lambda x: x.Channel.startswith('SIP/'), channels)))
    iax2_channels = len(
        list(filter(lambda x: x.Channel.startswith('IAX2/'), channels)))
    channels_current['sip'] = sip_channels
    channels_current['iax2'] = iax2_channels
    sip_channels and stats.gauge(
        'asterisk_channels_current', sip_channels, tags={'channel': 'sip'})
    iax2_channels and stats.gauge(
        'asterisk_channels_current', iax2_channels, tags={'channel': 'iax2'})

    # Get initial peers
    ShowSIPpeers = yield from manager.send_action({'Action': 'SIPpeers'})
    sip_peers = list(filter(lambda x: x.Event == 'PeerEntry', ShowSIPpeers))
    #logger.debug('sip_peers: {}'.format(sip_peers))

    ShowIAXpeers = yield from manager.send_action({'Action': 'IAXpeerlist'})
    #logger.debug('IAXpeerlist: {}'.format(ShowIAXpeers))
    iax_peers = list(filter(lambda x: x.Event == 'PeerEntry', ShowIAXpeers))
    #logger.debug('iax_peers: {}'.format(iax_peers))

    sip_peers_total = set(map(lambda p: p.Channeltype+'/'+p.ObjectName, sip_peers))
    logger.debug('sip_peers_set_total: {}'.format(sip_peers_total))
    sip_peers_total and stats.gauge('asterisk_total_peers', len(sip_peers_total), tags={'channel': 'sip'})

    iax_peers_total = set(map(lambda p: p.Channeltype+'/'+p.ObjectName, iax_peers))
    logger.debug('iax_peers_set_total: {}'.format(iax_peers_total))
    iax_peers_total and stats.gauge('asterisk_total_peers', len(iax_peers_total), tags={'channel': 'iax2'})

    sip_reachable_peers.update(set(map(lambda p: 'SIP/'+p.ObjectName,
                                       list(filter(lambda x: x.Status.startswith('OK'), sip_peers)))))
    sip_reachable_peers and stats.gauge('asterisk_reachable_peers', len(
        sip_reachable_peers), tags={'channel': 'sip'})
    logger.debug('sip_reachable_peers: {}'.format(sip_reachable_peers))

    iax_reachable_peers.update(set(map(lambda p: 'IAX2/'+p.ObjectName,
                                       list(filter(lambda x: x.Status.startswith('OK'), iax_peers)))))
    iax_reachable_peers and stats.gauge('asterisk_reachable_peers', len(
        iax_reachable_peers), tags={'channel': 'iax2'})
    logger.debug('iax_reachable_peers: {}'.format(iax_reachable_peers))


@manager.register_event('Newchannel')
def on_asterisk_Newchannel(manager, msg):
    channel = msg.Channel.split('/')[0].lower()
    stats.incr('asterisk_channels_total', tags={'channel': channel})
    if channels_current.get(channel) != None:
        channels_current[channel] += 1
    else:
        channels_current[channel] = 0
    logger.debug('New channel {}, current: {}'.format(
        channel, channels_current[channel]))
    stats.gauge('asterisk_channels_current', channels_current[channel],
                tags={'channel': channel})


@manager.register_event('Hangup')
def on_asterisk_Hangup(manager, msg):
    channel = msg.Channel.split('/')[0].lower()
    if channels_current.get(channel) != None:
        channels_current[channel] -= 1
    else:
        channels_current[channel] = 0
    logger.debug('Channel {} hangup, current: {}'.format(
        channel, channels_current[channel]))
    stats.gauge('asterisk_channels_current', channels_current[channel],
                tags={'channel': channel})


@manager.register_event('QueueCallerLeave')
@manager.register_event('QueueCallerJoin')
def on_asterisk_QueueCallerJoin(manager, msg):
    channel = ''.join(msg.Channel.split('-')[:-1])
    logger.debug('event: {}, channel: {}, queue: {}, position: {}, count: {}'.format(
        msg.Event, channel, msg.Queue, msg.Position, msg.Count))
    stats.gauge('asterisk_queue_callers', int(
        msg.Count), tags={'queue': msg.Queue})

# @manager.register_event('ContactStatus')
# def on_asterisk_ContactStatus(manager, msg):
#     if msg.ContactStatus == 'Reachable':
#         logger.debug('event: {}, status: {}, peer: {}, qualify: {}'.format(msg.Event, msg.ContactStatus, msg.EndpointName, msg.RoundtripUsec))
#         stats.gauge('asterisk_peer_qualify_seconds', float(msg.RoundtripUsec)/1000000, tags={'peer':msg.EndpointName})
#         sip_reachable_peers.add('PJSIP/'+msg.EndpointName)
#     elif msg.ContactStatus == 'Unreachable':
#         logger.debug('event: {}, status: {}, peer: {}, qualify: {}'.format(msg.Event, msg.ContactStatus, msg.EndpointName, msg.RoundtripUsec))
#         #stats.gauge('asterisk_peer_qualify_seconds', float(msg.RoundtripUsec)/1000000, tags={'peer':msg.EndpointName})
#         sip_reachable_peers.discard('PJSIP/'+msg.EndpointName)


@manager.register_event('PeerStatus')
def on_asterisk_PeerStatus(manager, msg):
    #logger.debug('SIP peers: {}, IAX peers: {}'.format(
    #    sip_reachable_peers, iax_reachable_peers))

    logger.debug('event: {}, peer: {}, channel: {},  status: {}'.format(
        msg.Event, msg.Peer, msg.ChannelType, msg.PeerStatus))
    if msg.PeerStatus in ['Reachable', 'Registered']:
        if msg.ChannelType == 'SIP':
            sip_reachable_peers.add(msg.Peer)
        if msg.ChannelType == 'IAX2':
            iax_reachable_peers.add(msg.Peer)

    elif msg.PeerStatus in ['Unreachable', 'Unregistered']:
        if msg.ChannelType == 'SIP':
            sip_reachable_peers.discard(msg.Peer)
        if msg.ChannelType == 'IAX2':
            iax_reachable_peers.discard(msg.Peer)

    logger.debug('SIP peers: {}, IAX peers: {}'.format(
        sip_reachable_peers, iax_reachable_peers))

    stats.gauge('asterisk_reachable_peers', len(sip_reachable_peers), tags={'channel': 'sip'})
    stats.gauge('asterisk_reachable_peers', len(iax_reachable_peers), tags={'channel': 'iax2'})


def on_asterisk_DialBegin(manager, msg):
    print(msg)


def on_asterisk_DialEnd(manager, msg):
    print(msg)


def on_asterisk_Reload(manager, msg):
    print(msg)


if __name__ == '__main__':
    main()
