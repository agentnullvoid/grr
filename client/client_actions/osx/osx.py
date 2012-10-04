#!/usr/bin/env python
# Copyright 2011 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""OSX specific actions."""



import ctypes
import logging
import os
import re
import sys


import pytsk3

from grr.client import actions
from grr.client import client_config
from grr.client import client_utils_common
from grr.client import client_utils_osx
from grr.client import conf
from grr.client.osx.objc import ServiceManagement
from grr.parsers import osx_launchd
from grr.proto import jobs_pb2
from grr.proto import sysinfo_pb2


class Error(Exception):
  """Base error class."""


class UnsupportedOSVersionError(Error):
  """This action not supported on this os version."""


# struct sockaddr_dl {
#       u_char  sdl_len;        /* Total length of sockaddr */
#       u_char  sdl_family;     /* AF_LINK */
#       u_short sdl_index;      /* if != 0, system given index for interface */
#       u_char  sdl_type;       /* interface type */
#       u_char  sdl_nlen;       /* interface name length, no trailing 0 reqd. */
#       u_char  sdl_alen;       /* link level address length */
#       u_char  sdl_slen;       /* link layer selector length */
#       char    sdl_data[12];   /* minimum work area, can be larger;
#                                  contains both if name and ll address */
#       u_short sdl_rcf;        /* source routing control */
#       u_short sdl_route[16];  /* source routing information */
# };


class Sockaddrdl(ctypes.Structure):
  """The sockaddr_dl struct."""
  _fields_ = [
      ("sdl_len", ctypes.c_ubyte),
      ("sdl_family", ctypes.c_ubyte),
      ("sdl_index", ctypes.c_ushort),
      ("sdl_type", ctypes.c_ubyte),
      ("sdl_nlen", ctypes.c_ubyte),
      ("sdl_alen", ctypes.c_ubyte),
      ("sdl_slen", ctypes.c_ubyte),
      ("sdl_data", ctypes.c_char * 12),
      ("sdl_rcf", ctypes.c_ushort),
      ("sdl_route", ctypes.c_char * 16)
      ]

# struct sockaddr_in {
#         __uint8_t       sin_len;
#         sa_family_t     sin_family;
#         in_port_t       sin_port;
#         struct  in_addr sin_addr;
#         char            sin_zero[8];
# };


class Sockaddrin(ctypes.Structure):
  """The sockaddr_in struct."""
  _fields_ = [
      ("sin_len", ctypes.c_ubyte),
      ("sin_family", ctypes.c_ubyte),
      ("sin_port", ctypes.c_ushort),
      ("sin_addr", ctypes.c_ubyte * 4),
      ("sin_zero", ctypes.c_char * 8)
      ]

# struct sockaddr_in6 {
#         __uint8_t       sin6_len;       /* length of this struct */
#         sa_family_t     sin6_family;    /* AF_INET6 (sa_family_t) */
#         in_port_t       sin6_port;      /* Transport layer port */
#         __uint32_t      sin6_flowinfo;  /* IP6 flow information */
#         struct in6_addr sin6_addr;      /* IP6 address */
#         __uint32_t      sin6_scope_id;  /* scope zone index */
# };


class Sockaddrin6(ctypes.Structure):
  """The sockaddr_in6 struct."""
  _fields_ = [
      ("sin6_len", ctypes.c_ubyte),
      ("sin6_family", ctypes.c_ubyte),
      ("sin6_port", ctypes.c_ushort),
      ("sin6_flowinfo", ctypes.c_ubyte * 4),
      ("sin6_addr", ctypes.c_ubyte * 16),
      ("sin6_scope_id", ctypes.c_ubyte * 4)
      ]


# struct ifaddrs   *ifa_next;         /* Pointer to next struct */
#          char             *ifa_name;         /* Interface name */
#          u_int             ifa_flags;        /* Interface flags */
#          struct sockaddr  *ifa_addr;         /* Interface address */
#          struct sockaddr  *ifa_netmask;      /* Interface netmask */
#          struct sockaddr  *ifa_broadaddr;    /* Interface broadcast address */
#          struct sockaddr  *ifa_dstaddr;      /* P2P interface destination */
#          void             *ifa_data;         /* Address specific data */


class Ifaddrs(ctypes.Structure):
  pass

Ifaddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(Ifaddrs)),
    ("ifa_name", ctypes.POINTER(ctypes.c_char)),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.POINTER(ctypes.c_char)),
    ("ifa_netmask", ctypes.POINTER(ctypes.c_char)),
    ("ifa_broadaddr", ctypes.POINTER(ctypes.c_char)),
    ("ifa_destaddr", ctypes.POINTER(ctypes.c_char)),
    ("ifa_data", ctypes.POINTER(ctypes.c_char))
    ]


class EnumerateInterfaces(actions.ActionPlugin):
  """Enumerate all MAC addresses of all NICs."""
  out_protobuf = jobs_pb2.Interface

  def Run(self, unused_args):
    """Enumerate all MAC addresses."""
    libc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("c"))
    ifa = Ifaddrs()
    p_ifa = ctypes.pointer(ifa)
    libc.getifaddrs(ctypes.pointer(p_ifa))

    addresses = {}
    macs = {}
    ifs = set()

    m = p_ifa
    while m:
      ifname = ctypes.string_at(m.contents.ifa_name)
      ifs.add(ifname)
      try:
        iffamily = ord(m.contents.ifa_addr[1])
        if iffamily == 0x2:     # AF_INET
          data = ctypes.cast(m.contents.ifa_addr, ctypes.POINTER(Sockaddrin))
          ip4 = "".join(map(chr, data.contents.sin_addr))
          address_type = jobs_pb2.NetworkAddress.INET
          address = jobs_pb2.NetworkAddress(address_type=address_type,
                                            packed_bytes=ip4)
          addresses.setdefault(ifname, []).append(address)

        if iffamily == 0x12:    # AF_LINK
          data = ctypes.cast(m.contents.ifa_addr, ctypes.POINTER(Sockaddrdl))
          iflen = data.contents.sdl_nlen
          addlen = data.contents.sdl_alen
          macs[ifname] = data.contents.sdl_data[iflen:iflen+addlen]

        if iffamily == 0x1E:     # AF_INET6
          data = ctypes.cast(m.contents.ifa_addr, ctypes.POINTER(Sockaddrin6))
          ip6 = "".join(map(chr, data.contents.sin6_addr))
          address_type = jobs_pb2.NetworkAddress.INET6
          address = jobs_pb2.NetworkAddress(address_type=address_type,
                                            packed_bytes=ip6)
          addresses.setdefault(ifname, []).append(address)
      except ValueError:
        # Some interfaces don't have a iffamily and will raise a null pointer
        # exception. We still want to send back the name.
        pass

      m = m.contents.ifa_next

    libc.freeifaddrs(p_ifa)

    for interface in ifs:
      mac = macs.setdefault(interface, "")
      address_list = addresses.setdefault(interface, "")
      args = {"ifname": interface}
      if mac:
        args["mac_address"] = mac
      if address_list:
        args["addresses"] = address_list
      self.SendReply(**args)


class GetInstallDate(actions.ActionPlugin):
  """Estimate the install date of this system."""
  out_protobuf = jobs_pb2.DataBlob

  def Run(self, unused_args):
    for f in ["/var/log/CDIS.custom", "/var", "/private"]:
      try:
        stat = os.stat(f)
        self.SendReply(integer=int(stat.st_ctime))
        return
      except OSError:
        pass
    self.SendReply(integer=0)


class EnumerateUsers(actions.ActionPlugin):
  """Enumerates all the users on this system."""
  out_protobuf = jobs_pb2.UserAccount

  def Run(self, unused_args):
    """Enumerate all users on this machine."""
    # TODO(user): Add /var/run/utmpx parsing as per linux
    blacklist = ["Shared"]
    for user in os.listdir("/Users"):
      userdir = "/Users/{0}".format(user)
      if user not in blacklist and os.path.isdir(userdir):
        self.SendReply(username=user, homedir=userdir)


class EnumerateFilesystems(actions.ActionPlugin):
  """Enumerate all unique filesystems local to the system."""
  out_protobuf = sysinfo_pb2.Filesystem

  def Run(self, unused_args):
    """List all local filesystems mounted on this system."""
    for fs_struct in client_utils_osx.GetFileSystems():
      self.SendReply(device=fs_struct.f_mntfromname,
                     mount_point=fs_struct.f_mntonname,
                     type=fs_struct.f_fstypename)

    drive_re = re.compile("r?disk[0-9].*")
    for drive in os.listdir("/dev"):
      if not drive_re.match(drive):
        continue

      path = os.path.join("/dev", drive)
      try:
        img_inf = pytsk3.Img_Info(path)
        # This is a volume or a partition - we send back a TSK device.
        self.SendReply(device=path)

        vol_inf = pytsk3.Volume_Info(img_inf)

        for volume in vol_inf:
          if volume.flags == pytsk3.TSK_VS_PART_FLAG_ALLOC:
            offset = volume.start * vol_inf.info.block_size
            self.SendReply(device=path + ":" + str(offset),
                           type="partition")

      except (IOError, RuntimeError):
        continue


class EnumerateRunningServices(actions.ActionPlugin):
  """Enumerate all running launchd jobs."""
  in_protobuf = None
  out_protobuf = sysinfo_pb2.Service

  def GetRunningLaunchDaemons(self):
    """Get running launchd jobs from objc ServiceManagement framework."""

    sm = ServiceManagement()
    return sm.SMGetAllJobDictionaries("kSMDomainSystemLaunchd")

  def Run(self, unused_arg):
    """Get running launchd jobs.

    Raises:
      UnsupportedOSVersionError: for OS X earlier than 10.6
    """

    self.osversion = client_utils_osx.OSXVersion().VersionAsFloat()

    if self.osversion < 10.6:
      raise UnsupportedOSVersionError(
          "ServiceManagment API unsupported on < 10.6. This"
          " client is %s" % self.osversion)

    launchd_list = self.GetRunningLaunchDaemons()

    self.parser = osx_launchd.OSXLaunchdJobDict(launchd_list)
    for job in self.parser.Parse():
      response = self.CreateServiceProto(job)
      self.SendReply(response)

  def _AddRepeatedField(self, job, keyname, protoaddcallback):
    if keyname in job:
      for item in job[keyname]:
        protoaddcallback(item)

  def CreateServiceProto(self, job):
    """Create the Service protobuf.

    Args:
      job: Launcdjobdict from servicemanagement framework.
    Returns:
      sysinfo_pb2.Service proto
    """
    job_pb = sysinfo_pb2.LaunchdJob(sessiontype=
                                    job.get("LimitLoadToSessionType", ""),
                                    lastexitstatus=job["LastExitStatus"],
                                    timeout=job["TimeOut"],
                                    ondemand=job["OnDemand"])

    args = map(str, job.get("ProgramArguments", ""))
    args_str = " ".join(args)

    self._AddRepeatedField(job, "MachServices",
                           job_pb.machservice.append)
    self._AddRepeatedField(job, "PerJobMachServices",
                           job_pb.perjobmachservice.append)

    service_pb = sysinfo_pb2.Service(label=job.get("Label", ""),
                                     program=job.get("Program", ""),
                                     args=args_str,
                                     osx_launchd=job_pb)

    if "PID" in job:
      service_pb.pid = job["PID"]

    return service_pb


class Uninstall(actions.ActionPlugin):
  """Remove the service that starts us at startup."""
  out_protobuf = jobs_pb2.DataBlob

  def Run(self, unused_arg):
    """This kills us with no cleanups."""
    logging.debug("Disabling service")

    if not conf.RUNNING_AS_SERVICE:
      self.SendReply(string="Not running as service.")
    else:
      plist = client_config.LAUNCHCTL_PLIST
      (_, _, result) = client_utils_common.Execute("/sbin/launchctl",
                                                   ["unload",
                                                    plist])

      if result != 0:
        self.SendReply(string="Service failed to disable.")
      else:
        logging.info("Disabled service successfully")
        self.SendReply(string="Service disabled.")

        os.remove(client_config.LAUNCHCTL_PLIST)

        if hasattr(sys, "frozen"):
          grr_binary = os.path.abspath(sys.executable)
        elif __file__:
          grr_binary = os.path.abspath(__file__)

        os.remove(grr_binary)