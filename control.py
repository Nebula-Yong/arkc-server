import logging
from collections import deque
from random import expovariate
from utils import weighted_choice
from twisted.internet import reactor
from twisted.internet.endpoints import TCP4ClientEndpoint, connectProtocol
from txsocksx.client import SOCKS5ClientEndpoint as SOCKS5Point
from txsocksx.client import SOCKS4ClientEndpoint as SOCKS4Point
import threading
import random
import os
import string
import sys

from proxy import ProxyConnector
from utils import addr_to_str
from client import ClientConnector
from meekserver import meekinit
import psutil
import atexit
EXPIRE_TIME = 5


def exit_handler():

    for proc in psutil.process_iter():
        # check whether the process name matches
        # TODO: figure out what's wrong with PT_PROC
        if proc.name() == "obfs4proxy" or proc.name() == "obfs4proxy.exe"\
                or proc.name() == "meek-client" or proc.name() == "meek-client.exe":
            proc.kill()


class Control:
    """The core part of the server, acting as a bridge between client and proxy.

    One Control instance corresponds to one client (with unique SHA1).
    Its functions include:
        - Maintain a certain number of connections to client
            through ClientConnectors, as specified in UDP message.
        - Reset and re-connect at a random period (between 30 to 90 secs),
            for better performance.
        - Stash request data from client. Create ProxyConnectors on demand,
            through which to send the data.
        - Stash reponse data from proxy. Pick a connection to client to
            send them back.
    The Control itself is passed to both ClientConnectors and ProxyConnectors
        spawned from it as the `initiator` parameter.
    """

    def __init__(self, initiator, client_sha1, client_pub, client_pri_sha1,
                 host, port, main_pw, req_num, certs_str=None):
        self.initiator = initiator
        self.socksproxy = self.initiator.socksproxy
        self.close_char = chr(4) * 5
        self.client_sha1 = client_sha1
        self.obfs_level = self.initiator.obfs_level
        self.client_pub = client_pub
        self.client_pri_sha1 = client_pri_sha1
        self.original_host = self.host = host
        self.original_port = self.port = port
        self.main_pw = main_pw
        self.req_num = req_num
        self.certs_str = certs_str
        self.max_retry = 5
        self.retry_count = 0
        self.swap_count = 0

        self.preferred_conn = None
        self.client_connectors_pool = [None] * req_num
        # each entry is a dict: conn_id -> queue
        # a queue is formed by (index, data) pairs in order
        self.client_buf_pool = [{}] * req_num

        # maps ID to ProxyConnectors
        self.proxy_connectors_dict = dict()

        # maps ID to decrypted data segments
        self.proxy_write_queues_dict = dict()
        self.proxy_write_queues_index_dict = dict()
        self.proxy_recv_index_dict = dict()

        self.max_index = dict()  # TODO: need clarification

        # Create an endpoint for the HTTP proxy
        host, port = "127.0.0.1", self.initiator.proxy_port
        self.proxy_point = TCP4ClientEndpoint(reactor, host, port)

        # ptproxy (obfs4)
        if self.certs_str:
            self.ptproxy_local_port = random.randint(30000, 40000)
            while self.ptproxy_local_port in initiator.usedports:
                self.ptproxy_local_port += 1
            initiator.usedports.append(self.ptproxy_local_port)
            pt = threading.Thread(
                target=self.ptinit)
            pt.setDaemon(True)
            self.check = threading.Event()
            pt.start()
            self.check.wait(100)

        # meek (GAE) init
        if self.obfs_level == 3:
            self.ptproxy_local_port = None
            self.check = threading.Event()
            meek_var = {
                "ptexec": self.initiator.pt_exec +
                " --url=" + self.initiator.meek_url +
                " --desturl=http://" + self.host + ":" + str(self.port) + "/",
                "localport": self.ptproxy_local_port,
                "LOCK": self.check
            }
            pt = threading.Thread(
                target=meekinit, args=[self, meek_var])
            pt.setDaemon(True)
            pt.start()
            self.check.wait(100)

        # reactor.callLater(1, self.broadcast)

    # def broadcast(self):
        # (Experimental function) tell the client what connections are valid
        # TODO: update this method
    #    if not all(_ is None for _ in self.client_connectors_pool):
    #        str_send = ''
    #        for i in self.used_id:
    #            str_send += i + ','
    #        str_send.rstrip(',')
            # self.client_write(str_send, '00', '050') # TODO: disabled
            # experimental function
    #    reactor.callLater(1, self.broadcast)

    def update(self, host, port, main_pw, req_num):
        # Update the info in control object, called when different data come in
        # by new requests
        if self.original_host != host or self.original_port != port:
            if not self.obfs_level:
                self.original_host = self.host = host
                self.original_port = self.port = port
                logging.info("client address change")
            else:
                logging.error("pt mode does not allow client address change")
        if self.req_num < req_num:
            # Reduce req_num when working is not permitted
            self.client_connectors_pool += [None] * (req_num - self.req_num)
        if self.main_pw != main_pw:
            self.main_pw = main_pw
            logging.info("main password change")

    def connect(self):
        """Connect client."""
        if any(_ is None for _ in self.client_connectors_pool):

            connector = ClientConnector(self)

            # connect through Tor if required, direct connection otherwise
            if self.socksproxy:
                proxy = random.choice(self.socksproxy)
                # Further settings and check
                socks_point = TCP4ClientEndpoint(reactor, proxy[0], proxy[1])
                point = SOCKS5Point(self.host, self.port, socks_point)
            elif self.obfs_level == 3:
                meek_point = TCP4ClientEndpoint(
                    reactor, "127.0.0.1", self.ptproxy_local_port)
                point = SOCKS4Point(self.host, self.port, meek_point)
            else:
                point = TCP4ClientEndpoint(reactor, self.host, self.port)

            deferred = connectProtocol(point, connector)
            # trigger success or failure action depending on the result
            deferred.addCallback(self.success)
            deferred.addErrback(lambda ignored: self.retry(connector))

    def retry(self, conn):
        """Triggered when a failure connecting client occurs.

        Decrement the number of available connections
        (which is pre-added when trying to connect),
        and retry until the max retry count is reached.
        """
        self.client_connectors_pool[conn.i] = None
        if self.retry_count < self.max_retry:
            host, port = self.host, self.port
            logging.warning("retry connection to %s:%d" % (host, port))
            self.retry_count += 1
            self.connect()
        elif all(_ is None for _ in self.client_connectors_pool):
            self.dispose()

    def success(self, conn):
        """Triggered when successfully connected to client.

        Reset retry count and continue adding connections until the required
        available connection number (specified by client through UDP message)
        is reached.
        Check authentication process later
        """
        self.retry_count = 0
        reactor.callLater(5, self.conn_check, conn)
        # Recheck
        self.connect()

    def conn_check(self, conn):
        """Test whether a connection is authenticated, if so,
        Reset the connection after a random time for better performance.
        """
        if conn:
            if conn.authenticated:
                # Reset the connection after a random time, no need if using
                # timeout enforced by GAE
                if self.obfs_level < 3:
                    expire_time = expovariate(1.0 / 60)
                    reactor.callLater(expire_time, self.client_reset, conn)
            else:
                conn.write(self.close_char, "00", "100000")
                if self.obfs_level == 3:
                    reactor.callLater(1, conn.close)
                else:
                    conn.close()
            # TODO: ADD to some black list?

    def add_cli(self, conn):
        assert self.client_connectors_pool[conn.i] == 1  # pending for auth
        self.client_connectors_pool[conn.i] = conn

    def remove_cli(self, conn):
        """Reset the state of a slot when authentication fails."""
        # assert self.client_connectors_pool[conn.i] == 1  # pending for auth
        # TODO: deal with this case correctly
        self.client_connectors_pool[conn.i] = None

    def update_max_idx(self, cc, max_recved_idx):
        """Remove completed buffer and retransmit the remaining."""
        i = self.client_connectors_pool.index(cc)
        buf = self.client_buf_pool[i]
        for cli_id in max_recved_idx:
            queue = buf[cli_id]
            while len(queue) and queue[0][0] <= max_recved_idx[cli_id]:
                queue.popleft()
            if len(queue):
                # retransmit
                for idx, data in queue:
                    self.client_write(data, cli_id, idx)
            else:
                if max_recved_idx[cli_id] == self.max_index.get(cli_id, None):
                    # completed, remove id
                    self.del_proxy_conn(cli_id)

    def client_recv(self, recv):
        """Handle request from client.

        Should be decrypted by ClientConnector first.
        """
        conn_id, index, data = recv[:2], int(recv[2:8]), recv[8:]
        logging.debug("received %d bytes from client key " % len(data) +
                      conn_id)
        if data == self.close_char:
            # close connection and remove the ID
            if conn_id in self.proxy_connectors_dict:
                logging.debug("close message from client key " + conn_id)
                conn = self.proxy_connectors_dict[conn_id]
                if conn.transport is None:
                    self.proxy_lost(conn_id)
                else:
                    conn.transport.loseConnection()
            else:
                logging.debug("closing non-existing connection")
        else:
            try:
                if conn_id not in self.proxy_connectors_dict:
                    self.new_proxy_conn(conn_id)
                    self.proxy_write_queues_dict[conn_id][index] = data
                    # proxy_write called later
                else:
                    self.proxy_write_queues_dict[conn_id][index] = data
                    self.proxy_write(conn_id)
            except KeyError:
                self.client_write(self.close_char, conn_id, "100000")

    def client_write(self, data, conn_id, assigned_index=None):
        """Pick a client connector and write the data.
        Triggered by proxy_recv or proxy_finish.
        """

        conns_avail = filter(
            lambda _: _ not in (None, 1), self.client_connectors_pool)
        if not len(conns_avail):
            if self.retry_count < self.max_retry and self.req_num == 1:
                logging.warning("no available socket")
                return reactor.callLater(1, lambda: self.client_write(data, conn_id, assigned_index))
            else:
                self.dispose()
            return
            # TODO: reload coordinator
        if conn_id not in self.proxy_recv_index_dict:
            self.proxy_recv_index_dict[conn_id] = 100000
        if self.swap_count <= 0 or not self.preferred_conn.authenticated:
            # TODO: better algorithm
            f = lambda c: 1.0 / (c.latency ** 2 + 1)
            self.preferred_conn = weighted_choice(conns_avail, f)
            self.preferred_conn.latency += 100
            self.swap_count = 8
        else:
            self.swap_count -= 1
        if assigned_index:
            self.preferred_conn.write(data, conn_id, assigned_index)
        else:
            index = self.proxy_recv_index_dict[conn_id]
            self.preferred_conn.write(data, conn_id, str(index))
            i = self.client_connectors_pool.index(self.preferred_conn)
            if conn_id not in self.client_buf_pool[i]:
                self.client_buf_pool[i][conn_id] = deque()
            self.client_buf_pool[i][conn_id].append((index, data))
            self.proxy_recv_index_dict[conn_id] += 1
            if self.proxy_recv_index_dict[conn_id] == 1000000:
                # TODO: raise exception / cut connection
                self.proxy_recv_index_dict[conn_id] = 100000

    def client_reset(self, conn):
        """Called after a random time to reset a existing connection to client.

        May result in better performance.
        """
        self.client_lost(conn)
        conn.write(self.close_char, "00", "100000")
        conn.authenticated = False
        reactor.callLater(0.1, self.client_reset_exec, conn)

    def client_reset_exec(self, conn):
        try:
            conn.close()
        except Exception:
            pass

    def client_lost(self, conn):
        """Triggered by a ClientConnector's connectionLost method.

        Remove the closed connection and retry creating it.
        """
        if conn in self.client_connectors_pool:
            i = self.client_connectors_pool.index(conn)
            self.client_connectors_pool[i] = None
        self.connect()

    def register(self):
        for i in range(self.req_num):
            if self.client_connectors_pool[i] == None:
                # stands for pending for connection success
                self.client_connectors_pool[i] = 1
                return i
        raise ValueError

    def new_proxy_conn(self, conn_id):
        """Create a connection to HTTP proxy corresponding to the given ID.

        Return a Deferred object of the proxy connector.
        """
        logging.info("adding connection id " + conn_id)
        try:
            assert conn_id not in self.proxy_write_queues_dict
            self.proxy_write_queues_dict[conn_id] = dict()
            self.proxy_write_queues_index_dict[conn_id] = 100000
            self.proxy_connectors_dict[conn_id] = ProxyConnector(self, conn_id)
            point, connector = self.proxy_point, self.proxy_connectors_dict[
                conn_id]
            d = connectProtocol(point, connector)
            d.addCallback(lambda ignored: self.proxy_write(conn_id))
            d.addErrback(lambda ignored: logging.error("cannot connect proxy"))
        except AssertionError:
            logging.error("duplicate id")

    def del_proxy_conn(self, conn_id):
        """Remove the given ID.

        Triggered when the ID is no longer in use.
        """
        logging.info("deleting connection id " + conn_id)
        try:
            assert self.proxy_write_queues_dict.pop(conn_id, None) is not None
            assert self.proxy_write_queues_index_dict.pop(
                conn_id, None) is not None

            for i in range(self.req_num):
                self.client_buf_pool[i].pop(conn_id, None)
            self.proxy_recv_index_dict.pop(conn_id, None)
            self.max_index.pop(conn_id, None)

            assert conn_id in self.proxy_connectors_dict
            tp = self.proxy_connectors_dict.pop(conn_id).transport
            if tp:
                tp.loseConnection()
        except AssertionError:
            logging.warning("deleting non-existing key %s" % conn_id)
        except KeyError:
            pass

    def proxy_write(self, conn_id):
        """Forward all the data pending for the ID to the HTTP proxy."""

        while conn_id in self.proxy_write_queues_dict and self.proxy_write_queues_index_dict[conn_id] in self.proxy_write_queues_dict[conn_id]:
            data = self.proxy_write_queues_dict[conn_id].pop(
                self.proxy_write_queues_index_dict[conn_id])
            self.next_write_index(conn_id)
            if data is not None and len(data) > 0:
                conn = self.proxy_connectors_dict[conn_id]
                if not conn.transport:
                    self.proxy_lost(conn_id)
                else:
                    logging.debug("sending %d bytes to proxy %s from id %s" % (
                        len(data),
                        addr_to_str(conn.transport.getPeer()),
                        conn_id))
                    conn.transport.write(data)
        if self.proxy_write_queues_index_dict[conn_id] + 7 in self.proxy_write_queues_dict[conn_id]:
            logging.debug("lost frame in connection " + conn_id)
            # TODO: Retransmission
            # self.proxy_connectors_dict[conn_id].transport.loseConnection()

    def proxy_recv(self, data, conn_id):
        """Call client_write on receiving data from proxy."""
        try:
            self.client_write(data, conn_id)
        except AssertionError:
            logging.error(
                "%i dumped from proxy for no connections with the client" %
                len(data))
            logging.error("related proxy connection closing")
            self.proxy_lost(conn_id)
        # TODO: Are all proxy connections closed correctly? Keep-alive ones?

    def proxy_lost(self, conn_id):
        """Deal with the situation when proxy connection is lost unexpectedly.

        That is when conn.transport becomes None.
        """
        # TODO: why does this happen?
        # conn = self.proxy_connectors_dict[conn_id]
        # conn.dead = True
        # logging.warning("proxy connection %s lost unexpectedly" % conn_id)
        # conn.respond()
        # self.proxy_finish(conn_id)
        pass

    def proxy_finish(self, conn_id):
        """Tell the client that a request has finished and record the max index.

        Called when proxy connection is lost.
        """
        self.client_write(self.close_char, conn_id)
        self.max_index[conn_id] = self.proxy_recv_index_dict[conn_id] - 1

    def next_write_index(self, conn_id):
        self.proxy_write_queues_index_dict[conn_id] += 1
        if self.proxy_write_queues_index_dict[conn_id] == 1000000:
                # TODO: raise exception / cut connection
            self.proxy_write_queues_index_dict[conn_id] = 100000

    def dispose(self):
        try:
            for i in self.client_connectors_pool:
                i.loseConnection()
            for i in self.proxy_connectors_dict:
                self.proxy_connectors_dict[i].loseConnection()
            self.client_connectors_pool = None
            self.proxy_connectors_dict = None
        except Exception:
            pass
        self.initiator.remove_ctl(self.client_sha1)

# TODO: use the same strategy for proxy and client, to avoid error with large
# upload files

    # TODO: This pt is not working
    def ptinit(self):
        atexit.register(exit_handler)
        path = os.path.split(os.path.realpath(sys.argv[0]))[0]
        with open(path + os.sep + "ptserver.py") as f:
            code = compile(f.read(), "ptserver.py", 'exec')
            globals = {
                "SERVER_string": self.host + ":" + str(self.port),
                "ptexec": self.initiator.pt_exec + " -logLevel=ERROR",
                "localport": self.ptproxy_local_port,
                "remoteaddress": self.host,
                "remoteport": self.port,
                "certs": self.certs_str,
                "LOCK": self.check,
                "IAT": self.initiator.obfs_level
            }
            self.host = "127.0.0.1"
            self.port = self.ptproxy_local_port
            exec(code, globals)
