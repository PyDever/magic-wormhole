from __future__ import print_function, unicode_literals
from attr import attrs, attrib
from automat import MethodicalMachine
from zope.interface import implementer
from twisted.internet.defer import Deferred, inlineCallbacks, returnValue
from twisted.python import log
from .._interfaces import IDilator, IDilationManager, ISend
from ..util import dict_to_bytes, bytes_to_dict
from ..observer import OneShotObserver
from .encode import to_be4
from .subchannel import (SubChannel, _SubchannelAddress, _WormholeAddress,
                         ControlEndpoint, SubchannelConnectorEndpoint,
                         SubchannelListenerEndpoint)
from .connector import Connector, parse_hint
from .roles import LEADER, FOLLOWER
from .connection import KCM, Ping, Pong, Open, Data, Close, Ack
from .inbound import Inbound
from .outbound import Outbound

class OldPeerCannotDilateError(Exception):
    pass

@attrs
@implementer(IDilationManager)
class _ManagerBase(object):
    _reactor = attrib()
    _eventual_queue = attrib()

    def __attrs_post_init__(self):
        self._got_versions_d = Deferred()

        self._started = False
        self._endpoints = OneShotObserver(self._eventual_queue)

        self._connection = None
        self._made_first_connection = False
        self._first_connected = OneShotObserver(self._eventual_queue)
        self._host_addr = _WormholeAddress()

        self._next_subchannel_id = 0 # increments by 2

        # I kept getting confused about which methods were for inbound data
        # (and thus flow-control methods go "out") and which were for
        # outbound data (with flow-control going "in"), so I split them up
        # into separate pieces.
        self._inbound = Inbound(self, self._host_addr)
        self._outbound = Outbound(self) # data goes from us to a remote peer

    def set_listener_endpoint(self, listener_endpoint):
        self._inbound.set_listener_endpoint(listener_endpoint)
    def set_subchannel_zero(self, scid0, sc0):
        self._inbound.set_subchannel_zero(scid0, sc0)

    def when_first_connected(self):
        return self._first_connected.when_fired()


    def send_dilation_phase(self, **fields):
        dilation_phase = self._next_dilation_phase
        self._next_dilation_phase += 1
        self._S.send("dilate-%d" % dilation_phase, dict_to_bytes(fields))

    def send_hints(self, hints): # from Connector
        self.send_dilation_phase(type="hints", hints=hints)


    # forward inbound-ish things to _Inbound
    def subchannel_pauseProducing(self, sc):
        self._inbound.subchannel_pauseProducing(sc)
    def subchannel_resumeProducing(self, sc):
        self._inbound.subchannel_resumeProducing(sc)
    def subchannel_stopProducing(self, sc):
        self._inbound.subchannel_stopProducing(sc)

    # forward outbound-ish things to _Outbound
    def subchannel_registerProducer(self, sc, producer, streaming):
        self._outbound.subchannel_registerProducer(sc, producer, streaming)
    def subchannel_unregisterProducer(self, sc):
        self._outbound.subchannel_unregisterProducer(sc)

    def send_open(self, scid):
        self._queue_and_send(Open, scid)
    def send_data(self, scid, data):
        self._queue_and_send(Data, scid, data)
    def send_close(self, scid):
        self._queue_and_send(Close, scid)

    def _queue_and_send(self, record_type, *args):
        r = self._outbound.build_record(record_type, *args)
        self._outbound.queue_record(r)
        if self._connection:
            self._send_record(r)

    def send_record(self, r):
        # Outbound uses this to send queued messages when the connection is
        # established
        self._connection.send_record(r) # may trigger pauseProducing

    def subchannel_closed(self, scid, sc):
        # let everyone clean up. This happens just after we delivered
        # connectionLost to the Protocol, except for the control channel,
        # which might get connectionLost later after they use ep.connect.
        # TODO: is this inversion a problem?
        self._inbound.subchannel_closed(scid, sc)
        self._outbound.subchannel_closed(scid, sc)


    def _start_connecting(self, role):
        self._connector = Connector(self._transit_key, self._relay_url, self,
                                    self._reactor, self._eventual_queue,
                                    self._no_listen, self._tor,
                                    self._timing, self._side,
                                    self._eventual_queue,
                                    role)
        self._connector.start()

    # our Connector calls these, through our connecting/connected state machine

    def _use_connection(self, c):
        self._connection = c
        self._inbound.use_connection(c)
        self._outbound.use_connection(c) # does c.registerProducer
        if not self._made_first_connection:
            self._made_first_connection = True
            self._first_connected.fire(None)

    def _stop_using_connection(self):
        # the connection is already lost by this point
        self._connection = None
        self._inbound.stop_using_connection()
        self._outbound.stop_using_connection() # does c.unregisterProducer

    # from our active Connection

    def got_record(self, r):
        # records with sequence numbers: always ack, ignore old ones
        if isinstance(r, (Open, Data, Close)):
            self.send_ack(r.seqnum) # always ack, even for old ones
            if self._inbound.is_record_old(r):
                return
            self._inbound.update_ack_watermark(r.seqnum)
            if isinstance(r, Open):
                self._inbound.handle_open(r.scid)
            elif isinstance(r, Data):
                self._inbound.handle_data(r.scid, r.data)
            else: # isinstance(r, Close)
                self._inbound.handle_close(r.scid)
        if isinstance(r, KCM):
            log.err("got unexpected KCM")
        elif isinstance(r, Ping):
            self.handle_ping(r.ping_id)
        elif isinstance(r, Pong):
            self.handle_pong(r.ping_id)
        elif isinstance(r, Ack):
            self._outbound.handle_ack(r.resp_seqnum) # retire queued messages
        else:
            log.err("received unknown message type {}".format(r))

    # pings, pongs, and acks are not queued
    def send_ping(self, ping_id):
        if self._connection:
            self._connection.send_record(Ping(ping_id))

    def send_pong(self, ping_id):
        if self._connection:
            self._connection.send_record(Pong(ping_id))

    def send_ack(self, resp_seqnum):
        if self._connection:
            self._connection.send_record(Ack(resp_seqnum))

    def handle_ping(self, ping_id):
        self.send_pong(ping_id)

    def handle_pong(self, ping_id):
        # TODO: update is-alive timer
        pass

    # subchannel maintenance
    def allocate_subchannel_id(self):
        raise NotImplemented # subclass knows if we're leader or follower

# current scheme:
# * only the leader sends DILATE, only follower sends PLEASE
# * follower sends PLEASE upon w.dilate
# * leader doesn't send DILATE until receiving PLEASE and local w.dilate
# * leader handles either order of (w.dilate, rx_PLEASE)
# * maybe signal warning if we stay in a "want" state for too long
# * after sending DILATE, leader sends HINTS without waiting for response
# * nobody sends HINTS until they're ready to receive
# * nobody sends HINTS unless they've called w.dilate()
# * nobody connects to inbound hints unless they've called w.dilate()
# * if leader calls w.dilate() but not follower, leader waits forever in
#   "want" (doesn't send anything)
# * if follower calls w.dilate() but not leader, follower waits forever
#   in "want", leader waits forever in "wanted"

# We're "idle" until all three of:
# 1: we receive the initial VERSION message and learn our peer's "side"
#    value (then we compare sides, and the higher one is "leader", and
#    the lower one is "follower")
# 2: the peer is capable of dilation, qv version["can-dilate"] which is
#    a list of integers, require some overlap, "1" is current
# 3: the local app calls w.dilate()

class ManagerLeader(_ManagerBase):
    m = MethodicalMachine()
    set_trace = getattr(m, "_setTrace", lambda self, f: None)

    @m.state(initial=True)
    def IDLE(self): pass # pragma: no cover

    @m.state()
    def WANTING(self): pass # pragma: no cover
    @m.state()
    def WANTED(self): pass # pragma: no cover
    @m.state()
    def CONNECTING(self): pass # pragma: no cover
    @m.state()
    def CONNECTED(self): pass # pragma: no cover
    @m.state(terminal=True)
    def STOPPED(self): pass # pragma: no cover

    @m.input()
    def start(self): pass # pragma: no cover
    @m.input()
    def rx_PLEASE(self): pass # pragma: no cover
    @m.input()
    def rx_DILATE(self): pass # pragma: no cover
    @m.input()
    def rx_HINTS(self, hint_message): pass # pragma: no cover

    @m.input()
    def connection_made(self, c): pass # pragma: no cover
    @m.input()
    def connection_lost(self): pass # pragma: no cover

    @m.input()
    def stop(self): pass # pragma: no cover

    # these Outputs behave differently for the Leader vs the Follower
    @m.output()
    def send_dilate(self):
        self.send_dilation_phase(type="dilate")

    @m.output()
    def start_connecting(self):
        self._start_connecting(LEADER)

    # these Outputs delegate to the same code in both the Leader and the
    # Follower, but they must be replicated here because the Automat instance
    # is on the subclass, not the shared superclass

    @m.output()
    def use_hints(self, hint_message):
        hint_objs = filter(lambda h: h, # ignore None, unrecognizable
                           [parse_hint(hs) for hs in hint_message["hints"]])
        self._connector.got_hints(hint_objs)
    @m.output()
    def stop_connecting(self):
        self._connector.stop()
    @m.output()
    def use_connection(self, c):
        self._use_connection(c)
    @m.output()
    def stop_using_connection(self):
        self._stop_using_connection()
    @m.output()
    def signal_error(self):
        pass # TODO
    @m.output()
    def signal_error_hints(self, hint_message):
        pass # TODO

    IDLE.upon(rx_HINTS, enter=STOPPED, outputs=[signal_error_hints]) # too early
    IDLE.upon(stop, enter=STOPPED, outputs=[])
    IDLE.upon(rx_PLEASE, enter=WANTED, outputs=[])
    IDLE.upon(start, enter=WANTING, outputs=[])
    WANTED.upon(start, enter=CONNECTING, outputs=[send_dilate,
                                                  start_connecting])
    WANTED.upon(stop, enter=STOPPED, outputs=[])
    WANTING.upon(rx_PLEASE, enter=CONNECTING, outputs=[send_dilate,
                                                       start_connecting])
    WANTING.upon(stop, enter=STOPPED, outputs=[])

    CONNECTING.upon(rx_HINTS, enter=CONNECTING, outputs=[use_hints])
    CONNECTING.upon(connection_made, enter=CONNECTED, outputs=[use_connection])
    CONNECTING.upon(stop, enter=STOPPED, outputs=[stop_connecting])
    # leader shouldn't be getting rx_DILATE, and connection_lost only happens
    # while connected

    CONNECTED.upon(rx_HINTS, enter=CONNECTED, outputs=[]) # too late, ignore
    CONNECTED.upon(connection_lost, enter=CONNECTING,
                   outputs=[stop_using_connection,
                            send_dilate,
                            start_connecting])
    CONNECTED.upon(stop, enter=STOPPED, outputs=[stop_using_connection])
    # shouldn't happen: rx_DILATE, connection_made

    # we should never receive DILATE, we're the leader
    IDLE.upon(rx_DILATE, enter=STOPPED, outputs=[signal_error])
    WANTED.upon(rx_DILATE, enter=STOPPED, outputs=[signal_error])
    WANTING.upon(rx_DILATE, enter=STOPPED, outputs=[signal_error])
    CONNECTING.upon(rx_DILATE, enter=STOPPED, outputs=[signal_error])
    CONNECTED.upon(rx_DILATE, enter=STOPPED, outputs=[signal_error])

    def allocate_subchannel_id(self):
        # scid 0 is reserved for the control channel. the leader uses odd
        # numbers starting with 1
        scid_num = self._next_outbound_seqnum + 1
        self._next_outbound_seqnum += 2
        return to_be4(scid_num)

class ManagerFollower(_ManagerBase):
    m = MethodicalMachine()
    set_trace = getattr(m, "_setTrace", lambda self, f: None)

    @m.state(initial=True)
    def IDLE(self): pass # pragma: no cover

    @m.state()
    def WANTING(self): pass # pragma: no cover
    @m.state()
    def CONNECTING(self): pass # pragma: no cover
    @m.state()
    def CONNECTED(self): pass # pragma: no cover
    @m.state(terminal=True)
    def STOPPED(self): pass # pragma: no cover

    @m.input()
    def start(self): pass # pragma: no cover
    @m.input()
    def rx_PLEASE(self): pass # pragma: no cover
    @m.input()
    def rx_DILATE(self): pass # pragma: no cover
    @m.input()
    def rx_HINTS(self, hint_message): pass # pragma: no cover

    @m.input()
    def connection_made(self, c): pass # pragma: no cover
    @m.input()
    def connection_lost(self): pass # pragma: no cover
    # follower doesn't react to connection_lost, but waits for a new LETS_DILATE

    @m.input()
    def stop(self): pass # pragma: no cover

    # these Outputs behave differently for the Leader vs the Follower
    @m.output()
    def send_please(self):
        self.send_dilation_phase(type="please")

    @m.output()
    def start_connecting(self):
        self._start_connecting(FOLLOWER)

    # these Outputs delegate to the same code in both the Leader and the
    # Follower, but they must be replicated here because the Automat instance
    # is on the subclass, not the shared superclass

    @m.output()
    def use_hints(self, hint_message):
        hint_objs = filter(lambda h: h, # ignore None, unrecognizable
                           [parse_hint(hs) for hs in hint_message["hints"]])
        self._connector.got_hints(hint_objs)
    @m.output()
    def stop_connecting(self):
        self._connector.stop()
    @m.output()
    def use_connection(self, c):
        self._use_connection(c)
    @m.output()
    def stop_using_connection(self):
        self._stop_using_connection()
    @m.output()
    def signal_error(self):
        pass # TODO
    @m.output()
    def signal_error_hints(self, hint_message):
        pass # TODO

    IDLE.upon(rx_HINTS, enter=STOPPED, outputs=[signal_error_hints]) # too early
    IDLE.upon(rx_DILATE, enter=STOPPED, outputs=[signal_error]) # too early
    # leader shouldn't send us DILATE before receiving our PLEASE
    IDLE.upon(stop, enter=STOPPED, outputs=[])
    IDLE.upon(start, enter=WANTING, outputs=[send_please])
    WANTING.upon(rx_DILATE, enter=CONNECTING, outputs=[start_connecting])
    WANTING.upon(stop, enter=STOPPED, outputs=[])

    CONNECTING.upon(rx_HINTS, enter=CONNECTING, outputs=[use_hints])
    CONNECTING.upon(connection_made, enter=CONNECTED, outputs=[use_connection])
    # shouldn't happen: connection_lost
    #CONNECTING.upon(connection_lost, enter=CONNECTING, outputs=[?])
    CONNECTING.upon(rx_DILATE, enter=CONNECTING, outputs=[stop_connecting,
                                                          start_connecting])
    # receiving rx_DILATE while we're still working on the last one means the
    # leader thought we'd connected, then thought we'd been disconnected, all
    # before we heard about that connection
    CONNECTING.upon(stop, enter=STOPPED, outputs=[stop_connecting])

    CONNECTED.upon(connection_lost, enter=WANTING, outputs=[stop_using_connection])
    CONNECTED.upon(rx_DILATE, enter=CONNECTING, outputs=[stop_using_connection,
                                                         start_connecting])
    CONNECTED.upon(rx_HINTS, enter=CONNECTED, outputs=[]) # too late, ignore
    CONNECTED.upon(stop, enter=STOPPED, outputs=[stop_using_connection])
    # shouldn't happen: connection_made

    # we should never receive PLEASE, we're the follower
    IDLE.upon(rx_PLEASE, enter=STOPPED, outputs=[signal_error])
    WANTING.upon(rx_PLEASE, enter=STOPPED, outputs=[signal_error])
    CONNECTING.upon(rx_PLEASE, enter=STOPPED, outputs=[signal_error])
    CONNECTED.upon(rx_PLEASE, enter=STOPPED, outputs=[signal_error])

    def allocate_subchannel_id(self):
        # the follower uses even numbers starting with 2
        scid_num = self._next_outbound_seqnum + 2
        self._next_outbound_seqnum += 2
        return to_be4(scid_num)

@attrs
@implementer(IDilator)
class Dilator(object):
    """I launch the dilation process.

    I am created with every Wormhole (regardless of whether .dilate()
    was called or not), and I handle the initial phase of dilation,
    before we know whether we'll be the Leader or the Follower. Once we
    hear the other side's VERSION message (which tells us that we have a
    connection, they are capable of dilating, and which side we're on),
    then we build a DilationManager and hand control to it.
    """

    _reactor = attrib()
    _eventual_queue = attrib()

    def __attrs_post_init__(self):
        self._got_versions_d = Deferred()

    def wire(self, sender):
        self._S = ISend(sender)

    # this is the primary entry point, called when w.dilate() is invoked
    def dilate(self):
        if not self._started:
            self._started = True
            self._start().addBoth(self._endpoints.fire)
        yield self._endpoints.when_fired()

    @inlineCallbacks
    def _start(self):
        # first, we wait until we hear the VERSION message, which tells us 1:
        # the PAKE key works, so we can talk securely, 2: their side, so we
        # know who will lead, and 3: that they can do dilation at all

        (role, dilation_version) = yield self._got_versions_d

        if not dilation_version: # 1 or None
            raise OldPeerCannotDilateError()

        if role is LEADER:
            self._manager = ManagerLeader(self._reactor, self._eventual_queue)
        else:
            self._manager = ManagerFollower(self._reactor, self._eventual_queue)

        # we could probably return the endpoints earlier
        yield self._manager.when_first_connected()
        # we can open subchannels as soon as we get our first connection
        peer_addr = _SubchannelAddress()
        control_ep = ControlEndpoint(peer_addr)
        scid0 = b"\x00\x00\x00\x00"
        sc0 = SubChannel(scid0, self._manager, self._host_addr, peer_addr)
        control_ep._subchannel_zero_opened(sc0)
        self._manager.set_subchannel_zero(scid0, sc0)

        connect_ep = SubchannelConnectorEndpoint(self)

        listen_ep = SubchannelListenerEndpoint(self, self._host_addr)
        self._manager.set_listener_endpoint(listen_ep)

        endpoints = (control_ep, connect_ep, listen_ep)
        returnValue(endpoints)

    # from Boss
    def got_wormhole_versions(self, our_side, their_side,
                              their_wormhole_versions):
        # this always happens before received_dilate
        my_role = LEADER if our_side > their_side else FOLLOWER
        dilation_version = None
        their_dilation_versions = their_wormhole_versions.get("can-dilate", [])
        if 1 in their_dilation_versions:
            dilation_version = 1
        self._got_versions_d.callback( (my_role, dilation_version) )

    def received_dilate(self, plaintext):
        # this receives new in-order DILATE-n payloads, decrypted but not
        # de-JSONed.
        message = bytes_to_dict(plaintext)
        type = message["type"]
        if type == "please":
            self._manager.rx_PLEASE(message)
        elif type == "dilate":
            self._manager.rx_DILATE(message)
        elif type == "connection-hints":
            self._manager.rx_HINTS(message)
        else:
            log.err("received unknown dilation message type: {}".format(message))
            return
