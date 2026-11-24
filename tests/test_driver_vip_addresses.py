"""Driver for exercising vip_addresses handling in ibm_cloud_fail_over.py.

No Pacemaker, no IBM Cloud and no root are needed in the default (mock) mode:
  * subprocess.run is replaced with a recorder, so the exact 'ip a add/del ...'
    commands the library would execute are captured and asserted on.
  * the IBM Cloud VPC API and the instance metadata service are faked.

Usage:
  python3 test_driver_vip_addresses.py # mock mode (any OS)
  sudo python3 test_driver_vip_addresses.py --live IFACE VIP[,VIP...]
      # Linux only: really runs 'ip a add/del' on IFACE and verifies it,
      # e.g.  --live lo 192.0.2.10,192.0.2.11   (192.0.2.0/24 is TEST-NET-1)
      # An entry may carry its own prefix, e.g. 192.0.2.12/24.

This file must stay valid on Python 3.9: no 'X | Y' type unions, no 'match'.
"""
import json
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

# ---------------------------------------------------------------------------
# Use the installed ibm_cloud_fail_over package if there is one; otherwise fall
# back to the repo's src/ layout.  append() (not insert) keeps an installed
# copy ahead of the checkout on sys.path.
# ---------------------------------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

# Import the submodule (not the package) because the tests patch names that
# live in it (run, v, ApiException) and the package __init__ does not export them.
from ibm_cloud_fail_over import ibm_cloud_fail_over as lib  # noqa: E402

# ---------------------------------------------------------------------------
# Test constants – match the two-node cluster described in the plugin docs.
# ---------------------------------------------------------------------------
EXT_IP_1 = "10.240.0.4"   # permanent private IP of Node A
EXT_IP_2 = "10.240.0.5"   # permanent private IP of Node B
IFACE    = "eth0"          # local network interface name
VIPS     = ["192.168.100.1", "192.168.100.2"]   # virtual IPs to assign


def ip_a_with(*addresses):
    """Return text shaped like 'ip a' output for a host owning `addresses`."""
    out = ("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
           "    inet 127.0.0.1/8 scope host lo\n"
           "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n")
    for a in addresses:
        out += "    inet {}/24 brd 10.240.0.255 scope global eth0\n".format(a)
    return out


IP_A_NEITHER = ip_a_with()   # a host that owns neither node address


def prefix(vip):
    """Return the address in the form the library hands to 'ip'.

    _update_os_iface_vip_addresses appends /32 to a bare IP and leaves an entry that
    already contains a '/' alone.  The tests need the same rule in order to
    build the command they expect, and to key the failure overrides in
    fake_run (which sees the prefixed form, not the bare IP).
    """
    return vip if "/" in vip else vip + "/32"


# ---------------------------------------------------------------------------
# FakeCloud – stand-in for the IBM Cloud VPC API
#
# The library calls self._make_api_request(method, path, ...) whenever it
# needs to talk to IBM Cloud.  FakeCloud.request() has the same signature and
# returns just enough data to let the library code proceed:
#
#   GET /routing_tables  → one table called "tbl"
#   GET /routes          → one route with next_hop == EXT_IP_1 in zone us-south-1
#   PATCH / POST / DELETE → empty dict (success, nothing to parse)
#
# self.calls records every (method, path) so tests can verify that a PATCH
# did or did not happen.
#
# self.fail_methods lets a test make a whole HTTP method fail (for example
# {"PATCH"}), which is how we simulate "the cloud rejected the route update"
# without needing a real error response.
# ---------------------------------------------------------------------------
class FakeCloud:
    """Fake VPC API: one routing table, one route (next hop ext_ip_1 by default)."""

    def __init__(self):
        self.calls = []            # list of (method, path) tuples
        self.bodies = []           # list of (method, body) tuples
        self.fail_methods = set()  # HTTP methods that should raise ApiException
        # Where the route points right now.  Tests set this to EXT_IP_2 to
        # model "the peer is currently active".
        self.route_next_hop = EXT_IP_1

    def request(self, method, path, headers=None, body=None):
        self.calls.append((method, path))
        self.bodies.append((method, body))
        if method in self.fail_methods:
            # Same exception type the real _make_api_request raises for a
            # non-2xx response, so the library's 'except ApiException' paths
            # are exercised exactly as in production.
            raise lib.ApiException("simulated {} failure".format(method))
        if method == "GET" and "/routes?" in path:
            return {"routes": [{
                "id": "r1", "name": "vip-route",
                "destination": "192.168.100.0/24",
                "zone": {"name": "us-south-1"},
                "advertise": True, "priority": 2,
                "next_hop": {"address": self.route_next_hop},
            }]}
        if method == "GET" and "/routing_tables?" in path:
            return {"routing_tables": [{"id": "rt1", "name": "tbl"}]}
        return {}   # PATCH, POST, DELETE – success with no body

    def patches(self):
        """Return only the PATCH calls recorded so far."""
        return [c for c in self.calls if c[0] == "PATCH"]

    def patched_next_hops(self):
        """Return the next_hop address sent in each PATCH body."""
        return [json.loads(b)["next_hop"]["address"]
                for m, b in self.bodies if m == "PATCH"]


# ---------------------------------------------------------------------------
# Base test class – sets up all mocks that every test needs
# ---------------------------------------------------------------------------
class Base(unittest.TestCase):

    def setUp(self):
        # Collects the exact command lists that fake_run receives.
        self.ip_cmds = []

        # Per-VIP return-code overrides, keyed by the PREFIXED address that
        # appears in the command (for example "192.168.100.1/32").  Leave
        # empty for the happy path; set ip_rc[prefix(vip)] = non-zero to
        # simulate 'ip a add/del' exiting with that code for that address.
        self.ip_rc = {}

        # What 'ip a' prints on the simulated host, and every 'ip a' call made.
        # set_local_ip() decides whether THIS node is ext_ip_1 or ext_ip_2 by
        # looking for those addresses in this text.  The default is a host that
        # owns neither (only loopback), so local_ip stays "" and the library
        # falls back to toggling.  Tests that care about "which node am I" use
        # as_node() to say so.  Keeping this out of ip_cmds means the existing
        # 'ip a add/del' assertions are unaffected.
        self.ip_a_output = IP_A_NEITHER
        self.ip_a_calls = []
        self.ip_a_rc = 0

        # -------------------------------------------------------------------
        # fake_run – replaces the library's 'run' function for every call it
        # makes.
        #
        # The command the library builds is:
        #   ["ip", "a", "add", "192.168.100.1/32", "dev", "eth0"]
        #      0     1     2            3             4      5
        #
        # so cmd[3] is the prefixed address and cmd[2] is the operation
        # ("add" for SET, "del" for UNSET).
        #
        # The library now calls run(..., check=False) and inspects
        # result.returncode itself, instead of relying on subprocess to raise.
        # fake_run therefore just returns a CompletedProcess carrying the
        # return code.  The check=True branch is kept so the fake still
        # behaves like the real subprocess.run if a future library change
        # goes back to check=True.
        # -------------------------------------------------------------------
        def fake_run(cmd, *a, **k):
            if list(cmd) == ["ip", "a"]:              # listing, not modifying
                self.ip_a_calls.append(list(cmd))
                if self.ip_a_rc and k.get("check"):
                    raise subprocess.CalledProcessError(self.ip_a_rc, cmd)
                return subprocess.CompletedProcess(
                    cmd, self.ip_a_rc, self.ip_a_output, "")
            self.ip_cmds.append(list(cmd))            # record the command
            rc = self.ip_rc.get(cmd[3], 0)            # look up failure override
            if rc and k.get("check"):                 # non-zero rc + check=True?
                raise subprocess.CalledProcessError(rc, cmd)
            return subprocess.CompletedProcess(cmd, rc, "", "")

        self.cloud = FakeCloud()

        # -------------------------------------------------------------------
        # mock.patch.object(target, attribute, ...) temporarily replaces
        # target.attribute with a fake for the duration of the test.
        #
        # Patch 1 – lib.run → fake_run
        # ----------------------------
        # The library file now has:
        #     from subprocess import CalledProcessError, run
        # That statement copies the function into the library module's own
        # namespace under the name 'run'.  The library has NO attribute called
        # 'subprocess' any more, so the old target (lib.subprocess) would fail
        # with AttributeError.  lib.run IS the name the library looks up each
        # time it executes 'run(...)', so replacing it there intercepts every
        # call.  This is the "patch where it is used, not where it is defined"
        # rule: patching subprocess.run itself would not affect the library,
        # because it already holds its own reference to the original function.
        #
        # Patch 2 – HAFailOver._make_api_request → FakeCloud.request
        # ------------------------------------------------------------
        # _make_api_request is an instance method on the HAFailOver class.
        # We patch it on the CLASS (lib.HAFailOver), not on any particular
        # instance, which means every instance created during the test will
        # have the fake version.  side_effect=lambda *a, **k: ... lets us
        # forward the call to self.cloud.request() so we can also record it.
        #
        # Patch 3 – HAFailOver.get_instance_metadata → fixed dict
        # ---------------------------------------------------------
        # get_instance_metadata() opens a TCP connection to IBM Cloud's IMDS
        # endpoint (api.metadata.cloud.ibm.com) to discover the VPC ID and
        # availability zone.  That only works inside an actual IBM Cloud VSI.
        # We replace it with return_value={"vpc": ..., "zone": ...} so the
        # library thinks it is running on a VSI in us-south-1 inside vpc-1.
        #
        # Patch 4 – lib.v → fixed version string
        # --------------------------------------
        # HAFailOver.__init__ logs f"Version: {v('ibm_cloud_fail_over')}".
        # An f-string is evaluated even when DEBUG is off, and v() is
        # importlib.metadata.version, which raises PackageNotFoundError when
        # the library is run from a source checkout instead of being
        # pip-installed.  Without this patch every HAFailOver() call in these
        # tests would fail on a machine that has not installed the package,
        # which defeats the purpose of a driver that runs anywhere.
        #
        # Patching the same class multiple times is perfectly fine: each
        # patch.object() call targets a different attribute name, so they are
        # independent and do not interfere.
        # -------------------------------------------------------------------
        for p in (
            mock.patch.object(lib, "run",
                              side_effect=fake_run),
            mock.patch.object(lib.HAFailOver, "_make_api_request",
                              side_effect=lambda *a, **k: self.cloud.request(*a, **k)),
            mock.patch.object(lib.HAFailOver, "get_instance_metadata",
                              return_value={"vpc": {"id": "vpc-1"},
                                            "zone": {"name": "us-south-1"}}),
            mock.patch.object(lib, "v", return_value="0.0.0-test"),
        ):
            # -------------------------------------------------------------------
            # mock.patch.object() returns a patcher object – think of it as a
            # recipe for the swap, not the swap itself.  Calling p.start()
            # actually performs the replacement: it saves the original value of
            # the attribute and installs the fake in its place.  From this point
            # forward, any code that reads that attribute gets the fake.
            #
            # self.addCleanup(p.stop) registers p.stop() to be called
            # automatically when the test finishes (pass or fail).  p.stop()
            # undoes the replacement, restoring the original value.  This means
            # each test starts with a clean slate and patches never leak into
            # other tests.
            #
            # The alternative is to use patch as a context manager (with mock.patch
            # ...) or as a decorator (@mock.patch ...), but those only work when
            # the entire test body is in one function.  Because setUp runs before
            # each test method and we want all the patches active throughout,
            # start/addCleanup is the right pattern here.
            # -------------------------------------------------------------------
            p.start()
            self.addCleanup(p.stop)

    def as_node(self, ip):
        """Make the simulated host look like the node that owns `ip`."""
        self.ip_a_output = ip_a_with(ip)

    # Convenience: call fail_over_cr_vip with sensible defaults.
    def vip_op(self, cmd, vips=",".join(VIPS)):
        return lib.fail_over_cr_vip(
            cmd, "https://us-south.iaas.cloud.ibm.com",
            EXT_IP_1, EXT_IP_2, "key", IFACE, vips,
        )

    # Build the expected list of recorded ip commands for a given operation.
    # op is "add" (SET) or "del" (UNSET); the address is expected in prefixed
    # form because that is what the library passes to 'ip'.
    def expected(self, op, vips=VIPS):
        return [["ip", "a", op, prefix(v), "dev", IFACE] for v in vips]


# ---------------------------------------------------------------------------
# TestUpdateOsRoutes – tests _update_os_iface_vip_addresses in isolation
#
# We instantiate HAFailOver directly and call the method ourselves instead of
# going through fail_over_cr_vip.  This lets us verify the OS-level behaviour
# independently of the cloud routing logic.
# ---------------------------------------------------------------------------
class TestUpdateOsRoutes(Base):
    """Direct calls to HAFailOver._update_os_iface_vip_addresses."""

    def make(self, vips):
        """Create a minimal HAFailOver instance with only the fields
        _update_os_iface_vip_addresses reads."""
        h = lib.HAFailOver()
        h.interface_name = IFACE
        h.vip_addresses  = vips
        return h

    def test_set_adds_each_vip(self):
        # SET should produce 'ip a add <vip>/32 dev <iface>' for every VIP.
        self.make(VIPS)._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_unset_deletes_each_vip(self):
        # UNSET should produce 'ip a del <vip>/32 dev <iface>' for every VIP.
        self.make(VIPS)._update_os_iface_vip_addresses("UNSET")
        self.assertEqual(self.ip_cmds, self.expected("del"))

    def test_empty_list_runs_nothing(self):
        # When vip_addresses is empty the method must not call run at all.
        self.make([])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds, [])

    def test_empty_entries_are_skipped(self):
        # fail_over_cr_vip builds the list with vip_addresses.split(','), so a
        # trailing comma or a doubled comma yields '' entries.  Running
        # 'ip a add /32 dev eth0' for those would fail on a real host, so the
        # library filters them out.  Only the one real VIP may produce a
        # command.
        self.make(["", VIPS[0], ""])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds, self.expected("add", [VIPS[0]]))

    def test_bare_ip_gets_slash_32(self):
        # A VIP is a single address, so a bare IP must be given a /32 prefix.
        # Without a prefix 'ip' would pick a default that depends on the
        # address class, which could create a surprising connected route.
        self.make([VIPS[0]])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds[0][3], VIPS[0] + "/32")

    def test_entry_with_prefix_is_passed_through_unchanged(self):
        # If the user already supplied a prefix length, the library must not
        # append /32 on top of it (that would give '192.168.100.1/24/32').
        self.make(["192.168.100.1/24"])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds[0][3], "192.168.100.1/24")

    def test_add_rc2_means_already_assigned_and_continues(self):
        # 'ip a add' exits with 2 when the address is already on the
        # interface.  That is the normal situation when start is retried or
        # when Pacemaker re-runs start after a partial failure, so the
        # library logs it and moves on.  Both VIPs must still be attempted and
        # no exception may escape.
        self.ip_rc[prefix(VIPS[0])] = 2
        self.make(VIPS)._update_os_iface_vip_addresses("SET")   # must not raise
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_add_other_failure_raises_and_stops(self):
        # Any non-zero exit other than 2 on 'add' is a real error (bad
        # interface name, no permission, ...).  The library raises
        # CalledProcessError so the caller can fail the resource, and it
        # stops at the first failure: the second VIP is never attempted.
        self.ip_rc[prefix(VIPS[0])] = 1
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            self.make(VIPS)._update_os_iface_vip_addresses("SET")
        self.assertEqual(ctx.exception.returncode, 1)
        self.assertEqual(self.ip_cmds, self.expected("add", VIPS[:1]))

    def test_del_failure_raises_and_stops(self):
        # Same fail-fast rule for UNSET: if removing the first VIP fails, the
        # second is left on the interface and the error propagates.
        self.ip_rc[prefix(VIPS[0])] = 1
        with self.assertRaises(subprocess.CalledProcessError):
            self.make(VIPS)._update_os_iface_vip_addresses("UNSET")
        self.assertEqual(self.ip_cmds, self.expected("del", VIPS[:1]))

    def test_del_rc2_means_already_gone_and_continues(self):
        # 'ip a del' of an address that is no longer on the interface exits
        # with 2.  OCF requires stop to be idempotent, so the library treats
        # that like the 'add' case: log it and move on.  Both VIPs must still
        # be attempted and no exception may escape.
        self.ip_rc[prefix(VIPS[0])] = 2
        self.make(VIPS)._update_os_iface_vip_addresses("UNSET")   # must not raise
        self.assertEqual(self.ip_cmds, self.expected("del"))

    def test_whitespace_only_entry_is_not_stripped_here_characterization(self):
        # CHARACTERIZATION.  Stripping is done by fail_over_cr_vip when it
        # builds the list (see TestFailOverCrVip), not by this method, which
        # only drops entries that are exactly ''.  A whitespace-only entry
        # handed straight to this method therefore still reaches 'ip'.  This
        # pins that so moving the stripping is a deliberate decision.
        self.make([" "])._update_os_iface_vip_addresses("SET")
        self.assertEqual(len(self.ip_cmds), 1)


# ---------------------------------------------------------------------------
# TestSetLocalIp – HAFailOver.set_local_ip in isolation
#
# set_local_ip decides "which node am I" by looking for ext_ip_1 / ext_ip_2 in
# the output of 'ip a'.  When it fails to find either, local_ip stays "" and the
# route update toggles to the PEER.  That toggle is the bug that hit an already
# active node, so every branch here matters.
# ---------------------------------------------------------------------------
class TestSetLocalIp(Base):

    def make(self):
        h = lib.HAFailOver()
        h.ext_ip_1 = EXT_IP_1
        h.ext_ip_2 = EXT_IP_2
        return h

    def test_local_ip_starts_empty(self):
        # The default is the value that caused the bug; pin it so a change to
        # the default is noticed.
        self.assertEqual(self.make().local_ip, "")

    def test_host_owning_ext_ip_1(self):
        self.as_node(EXT_IP_1)
        h = self.make()
        h.set_local_ip()
        self.assertEqual(h.local_ip, EXT_IP_1)
        self.assertEqual(self.ip_a_calls, [["ip", "a"]])

    def test_host_owning_ext_ip_2(self):
        self.as_node(EXT_IP_2)
        h = self.make()
        h.set_local_ip()
        self.assertEqual(h.local_ip, EXT_IP_2)

    def test_host_owning_neither_leaves_local_ip_empty(self):
        h = self.make()
        h.set_local_ip()
        self.assertEqual(h.local_ip, "")

    def test_host_owning_both_prefers_ext_ip_1(self):
        self.ip_a_output = ip_a_with(EXT_IP_1, EXT_IP_2)
        h = self.make()
        h.set_local_ip()
        self.assertEqual(h.local_ip, EXT_IP_1)

    def test_empty_ext_ips_never_match(self):
        # '' is a substring of every string, so an unguarded "'' in ip_a"
        # would claim to be ext_ip_1.  This is exactly how an unset
        # ext_ip_1/ext_ip_2 must NOT behave.
        self.as_node(EXT_IP_1)
        h = lib.HAFailOver()          # ext_ip_1 and ext_ip_2 both ""
        h.set_local_ip()
        self.assertEqual(h.local_ip, "")

    def test_ip_a_failure_raises_api_exception(self):
        self.ip_a_rc = 1
        with self.assertRaises(lib.ApiException):
            self.make().set_local_ip()

    @unittest.expectedFailure
    def test_ip_prefix_of_another_address_is_not_a_match(self):
        # KNOWN GAP: set_local_ip does a substring test, so a host that owns
        # 10.240.0.45 is taken to own ext_ip_1 '10.240.0.4'.  The node would
        # then point the route at the wrong address.  Fix: match whole
        # addresses, for example with a regex on r'inet {ip}/' or by parsing
        # 'ip -o addr'.
        self.as_node("10.240.0.45")
        h = self.make()
        h.set_local_ip()
        self.assertEqual(h.local_ip, "")


# ---------------------------------------------------------------------------
# TestFailOverCrVip – end to end through the public fail_over_cr_vip function
# ---------------------------------------------------------------------------
class TestFailOverCrVip(Base):
    """End to end through fail_over_cr_vip with a fake cloud."""

    def test_set_patches_route_then_adds_vips(self):
        # SET must flip the cloud route (one PATCH) AND assign the VIPs locally.
        result = self.vip_op("SET")
        self.assertEqual(result, EXT_IP_2)              # next hop now points to Node B
        self.assertEqual(len(self.cloud.patches()), 1)  # exactly one cloud PATCH
        self.assertEqual(self.ip_cmds, self.expected("add"))

    # --- which node am I: the regression for 'start on the active node
    # --- switched the route to the peer' ---------------------------------

    def test_set_on_active_node_does_not_move_route_to_peer(self):
        # THE REGRESSION.  This node is ext_ip_1 and the route already points
        # at it.  With local_ip == "" the library toggled the route to
        # ext_ip_2 (the peer).  It must leave the route alone.
        self.as_node(EXT_IP_1)
        self.cloud.route_next_hop = EXT_IP_1
        result = self.vip_op("SET")
        self.assertEqual(result, EXT_IP_1)
        self.assertEqual(self.cloud.patched_next_hops(), [])

    def test_set_on_active_node_2_does_not_move_route_to_peer(self):
        # Same, for the other node: the bug must not be specific to ext_ip_1.
        self.as_node(EXT_IP_2)
        self.cloud.route_next_hop = EXT_IP_2
        result = self.vip_op("SET")
        self.assertEqual(result, EXT_IP_2)
        self.assertEqual(self.cloud.patched_next_hops(), [])

    def test_set_on_standby_node_1_takes_over_route(self):
        # This node is ext_ip_1, the route points at the peer: take it over.
        self.as_node(EXT_IP_1)
        self.cloud.route_next_hop = EXT_IP_2
        result = self.vip_op("SET")
        self.assertEqual(result, EXT_IP_1)
        self.assertEqual(self.cloud.patched_next_hops(), [EXT_IP_1])
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_set_on_standby_node_2_takes_over_route(self):
        self.as_node(EXT_IP_2)
        self.cloud.route_next_hop = EXT_IP_1
        result = self.vip_op("SET")
        self.assertEqual(result, EXT_IP_2)
        self.assertEqual(self.cloud.patched_next_hops(), [EXT_IP_2])
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_repeated_set_is_idempotent(self):
        # Pacemaker may call start more than once.  After the first SET moves
        # the route here, a second SET must not move it again.
        self.as_node(EXT_IP_2)
        self.cloud.route_next_hop = EXT_IP_1
        self.vip_op("SET")
        self.cloud.route_next_hop = EXT_IP_2    # the cloud now reflects the PATCH
        self.cloud.calls[:] = []
        self.cloud.bodies[:] = []
        self.assertEqual(self.vip_op("SET"), EXT_IP_2)
        self.assertEqual(self.cloud.patched_next_hops(), [])

    def test_set_looks_up_local_address_before_touching_the_cloud(self):
        # Ordering: set_local_ip must run before the route is evaluated, or
        # the toggle fallback is used.  'ip a' is the very first thing run.
        self.as_node(EXT_IP_1)
        self.vip_op("SET")
        self.assertEqual(self.ip_a_calls, [["ip", "a"]])

    def test_set_on_host_with_unknown_address_falls_back_to_toggle(self):
        # Characterization of the fallback: if this host owns neither node
        # address (misconfigured ext_ip_*), the old toggle behaviour applies.
        self.cloud.route_next_hop = EXT_IP_1
        result = self.vip_op("SET")
        self.assertEqual(result, EXT_IP_2)
        self.assertEqual(self.cloud.patched_next_hops(), [EXT_IP_2])

    def test_set_fails_when_ip_a_fails(self):
        # Better to fail the resource than to guess and move the route.
        self.ip_a_rc = 1
        with self.assertRaises(lib.ApiException):
            self.vip_op("SET")
        self.assertEqual(self.cloud.patches(), [])
        self.assertEqual(self.ip_cmds, [])

    def test_get_does_not_depend_on_local_ip(self):
        # GET only reports the next hop, whichever node asks.
        self.as_node(EXT_IP_2)
        self.cloud.route_next_hop = EXT_IP_1
        self.assertEqual(self.vip_op("GET"), EXT_IP_1)
        self.assertEqual(self.cloud.patches(), [])

    @unittest.expectedFailure
    def test_set_on_active_node_still_assigns_vips(self):
        # KNOWN GAP: when the route already points at this node,
        # update_vpc_routing_table_route returns early ("Route update
        # unnecessary") BEFORE _update_os_iface_vip_addresses runs.  If the
        # VIPs are missing from the interface (reboot, manual 'ip a del', or a
        # start after a stop that removed them), start on the active node
        # succeeds without ever assigning them.  Before the local_ip helper
        # this path was hidden by the toggle.  Fix: assign the VIPs on SET
        # even when no route PATCH is needed.
        self.as_node(EXT_IP_1)
        self.cloud.route_next_hop = EXT_IP_1
        self.vip_op("SET")
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_set_with_api_failure_adds_no_vips(self):
        # Order matters: the library updates the cloud route first and assigns
        # the VIPs locally only after that succeeds.  If the cloud rejects the
        # PATCH, this node must NOT end up holding the VIPs, otherwise both
        # nodes could answer for the same address.  The ApiException must
        # reach the caller and no 'ip' command may have run.
        self.cloud.fail_methods = {"PATCH"}
        with self.assertRaises(lib.ApiException):
            self.vip_op("SET")
        self.assertEqual(self.ip_cmds, [])

    def test_get_returns_next_hop_and_touches_nothing(self):
        # GET is read-only: no cloud PATCH, no ip commands.
        result = self.vip_op("GET")
        self.assertEqual(result, EXT_IP_1)
        self.assertEqual(self.ip_cmds, [])
        self.assertEqual(self.cloud.patches(), [])

    def test_unset_deletes_vips(self):
        # UNSET must remove the VIP addresses from the local interface.
        self.vip_op("UNSET")
        self.assertEqual(self.ip_cmds, self.expected("del"))

    def test_unset_should_not_modify_the_cloud_route(self):
        # This used to be an expected failure: UNSET fell into the PATCH
        # branch and flipped the next hop, redirecting traffic away from the
        # node that was about to take over.  update_vpc_routing_table_route now
        # returns early for UNSET, so nothing at all may be sent to the cloud
        # (not even the GETs) and the returned next hop is the empty string.
        result = self.vip_op("UNSET")
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(result, "")

    def test_empty_vip_addresses_runs_no_ip_command(self):
        # This used to be an expected failure: "".split(',') returns [''], a
        # list with one empty string, and the library ran
        # 'ip addr add "" dev eth0'.  Empty entries are now filtered out, so
        # a resource configured without VIPs does only the cloud route update.
        self.vip_op("SET", vips="")
        self.assertEqual(self.ip_cmds, [])
        self.assertEqual(len(self.cloud.patches()), 1)   # route still flipped

    def test_empty_vip_addresses_unset_runs_no_ip_command(self):
        # The same filtering must apply on the way down, otherwise stop would
        # fail for every resource that has no vip_addresses configured.
        self.vip_op("UNSET", vips="")
        self.assertEqual(self.ip_cmds, [])

    # --- parsing of the vip_addresses string by fail_over_cr_vip -----------
    # fail_over_cr_vip strips each entry and drops the empty ones before the
    # list reaches HAFailOver.  These tests go through it, because testing
    # _update_os_iface_vip_addresses alone cannot see that step.  Each one
    # fails if the parsing goes back to a bare vip_addresses.split(',').

    def test_whitespace_only_vip_addresses_runs_no_ip_command(self):
        # A resource parameter of " " is truthy, so only strip() removes it.
        # Without that, the library ran 'ip a add  /32 dev eth0'.
        for vips in (" ", "   ", "\t", "\n"):
            self.ip_cmds[:] = []
            self.vip_op("SET", vips=vips)
            self.assertEqual(self.ip_cmds, [], repr(vips))

    def test_whitespace_only_vip_addresses_unset_runs_no_ip_command(self):
        for vips in (" ", "\t"):
            self.vip_op("UNSET", vips=vips)
            self.assertEqual(self.ip_cmds, [], repr(vips))

    def test_whitespace_after_comma_is_stripped(self):
        # The natural way to write a list: "a, b".
        self.vip_op("SET", vips=VIPS[0] + ", " + VIPS[1])
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_whitespace_before_comma_and_around_list_is_stripped(self):
        self.vip_op("SET", vips="  " + VIPS[0] + " ,\t" + VIPS[1] + "  ")
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_whitespace_is_stripped_on_unset_too(self):
        self.vip_op("UNSET", vips=VIPS[0] + ", " + VIPS[1])
        self.assertEqual(self.ip_cmds, self.expected("del"))

    def test_trailing_leading_and_doubled_commas_are_ignored(self):
        for vips in (VIPS[0] + "," + VIPS[1] + ",",
                     "," + VIPS[0] + "," + VIPS[1],
                     VIPS[0] + ",," + VIPS[1],
                     VIPS[0] + ", ," + VIPS[1]):    # entry that is only a space
            self.ip_cmds[:] = []
            self.vip_op("SET", vips=vips)
            self.assertEqual(self.ip_cmds, self.expected("add"), repr(vips))

    def test_only_commas_runs_no_ip_command(self):
        for vips in (",", ",,", " , , "):
            self.vip_op("SET", vips=vips)
            self.assertEqual(self.ip_cmds, [], repr(vips))

    def test_padded_entry_with_prefix_keeps_its_prefix(self):
        # Stripping must happen before the '/' check, otherwise the padded
        # entry is still recognised but the command carries the padding.
        self.vip_op("SET", vips=" 192.168.100.1/24 , " + VIPS[1])
        self.assertEqual(self.ip_cmds[0], ["ip", "a", "add", "192.168.100.1/24", "dev", IFACE])
        self.assertEqual(self.ip_cmds[1], ["ip", "a", "add", prefix(VIPS[1]), "dev", IFACE])

    def test_empty_vips_do_not_prevent_the_route_update(self):
        # Parsing an empty/blank VIP string must not short-circuit SET: the
        # cloud route is still updated, only the local 'ip' step is skipped.
        self.vip_op("SET", vips=" , ")
        self.assertEqual(len(self.cloud.patches()), 1)
        self.assertEqual(self.ip_cmds, [])

    @unittest.expectedFailure
    def test_space_separated_vips_are_split(self):
        # KNOWN GAP: the library splits vip_addresses on ',' only (its
        # docstring says "comma-separated"), but the OCF RA convention seen in
        # gcp-vpc-move-vip uses spaces as the delimiter.  The two must agree.
        # Fix: either change the library split to whitespace, or document
        # comma as the only supported delimiter and delete this test.
        self.vip_op("SET", vips="192.168.100.1 192.168.100.2")
        self.assertEqual(self.ip_cmds, self.expected("add"))


# ---------------------------------------------------------------------------
# live() – optional mode that really runs ip a add/del on a Linux host.
# No mocks except the version lookup: calls _update_os_iface_vip_addresses directly and
# checks the kernel address table with 'ip -o addr show' to confirm the
# assignment took effect.
# Requires root (sudo) and a Linux host with the 'ip' command.
# Use TEST-NET addresses (192.0.2.0/24) to avoid conflicts with real traffic.
# ---------------------------------------------------------------------------
def live(iface, vips):
    """Really run ip a add/del on a Linux host and verify with 'ip'."""

    def present(vip):
        """Return True if vip is currently assigned to iface."""
        out = subprocess.run(
            ["ip", "-o", "addr", "show", "dev", iface],
            capture_output=True, text=True, check=True,
        ).stdout
        # 'ip -o addr show' prints one address per line; each line contains
        # the address in CIDR form, e.g. "2: eth0    inet 192.0.2.10/32 ...".
        # An entry given with its own prefix (192.0.2.12/24) must be compared
        # by address only, so drop the prefix before looking for it.
        address = vip.split("/")[0]
        return any(" {}/".format(address) in line for line in out.splitlines())

    def attempt(cmd):
        """Run one _update_os_iface_vip_addresses call and report the exit code of any
        failure instead of letting the exception end the live run."""
        try:
            h._update_os_iface_vip_addresses(cmd)
            return None
        except subprocess.CalledProcessError as exc:
            return exc.returncode

    # HAFailOver.__init__ calls importlib.metadata.version(), which fails when
    # the library is run from a source checkout that is not pip-installed.
    # This is the only thing faked in live mode.
    with mock.patch.object(lib, "v", return_value="live"):
        h = lib.HAFailOver()
    h.interface_name = iface
    h.vip_addresses  = vips
    ok = True

    # Phase 1 – the basic cycle: add everything, then delete everything.
    for step, cmd, want in (("add", "SET", True), ("del", "UNSET", False)):
        rc = attempt(cmd)
        if rc is not None:
            ok = False
            print("FAIL: {} raised, 'ip' exit code {}".format(step, rc))
            continue
        for v in vips:
            got = present(v)
            ok &= (got == want)
            print("{}: after {}, {} on {} present={} (expected {})".format(
                "PASS" if got == want else "FAIL", step, v, iface, got, want))

    # Phase 2 – repeating SET must succeed.  The second 'ip a add' exits with
    # 2 (address exists) and the library is supposed to tolerate that.
    attempt("SET")
    rc = attempt("SET")
    ok &= (rc is None)
    print("{}: second SET did not raise (rc={})".format(
        "PASS" if rc is None else "FAIL", rc))

    # Phase 3 – clean up, then repeat UNSET on the now-empty interface.  This
    # is a finding, not a pass/fail check: it reveals the real 'ip a del' exit
    # code for a missing address, which decides whether stop is idempotent
    # (see test_del_rc2_is_not_tolerated_characterization).
    attempt("UNSET")
    rc = attempt("UNSET")
    if rc is None:
        print("FINDING: second UNSET succeeded; stop is idempotent")
    else:
        print("FINDING: second UNSET raised, 'ip a del' exit code {}; "
              "stop is NOT idempotent for already-removed VIPs".format(rc))

    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--live":
        if len(sys.argv) != 4:
            sys.exit(__doc__)
        sys.exit(live(sys.argv[2], sys.argv[3].split(",")))
    unittest.main(verbosity=2)
