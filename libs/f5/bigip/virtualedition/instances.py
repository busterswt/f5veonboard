#!/usr/bin/env python

import os
import sys
import json
import re
import argparse
import termios  # @UnresolvedImport
import fcntl
import getpass
import prettytable
import time
import socket

import glanceclient
from os import environ as env
from distutils.util import strtobool
from keystoneclient.v2_0 import client as authclient
from novaclient import client as compclient
from cinderclient.v1 import client as volclient
from neutronclient.neutron import client as netclient
from uuid import UUID
from f5.bigip import bigip
from f5.common import constants as f5const


class F5Manager():
    _ks_client = None
    _g_client = None
    _n_client = None
    _c_client = None
    _net_client = None
    _auth_token = None

    _discovered_tmos_disk_images = {}
    _discovered_tmos_volume_images = {}
    _discovered_tmos_flavors = {}
    _flavor_list = []

    def __init__(self):
        self._discover_tmos_flavors()

    def _get_ksclient(self):
        """Get Keystone Client."""
        if not self._ks_client:
            self._ks_client = authclient.Client(
                                    username=env['OS_USERNAME'],
                                    password=env['OS_PASSWORD'],
                                    tenant_name=env['OS_TENANT_NAME'],
                                    auth_url=env['OS_AUTH_URL'])
        return self._ks_client

    def _get_auth_token(self):
        """Get Keyston Auth Token"""
        if not self._auth_token:
            self._auth_token = self._get_ksclient().auth_token
        return self._auth_token

    def _strip_version(self, endpoint):
        """Strip version from the last component of endpoint if present."""
        # Get rid of trailing '/' if present
        if endpoint.endswith('/'):
            endpoint = endpoint[:-1]
        url_bits = endpoint.split('/')
        # regex to match 'v1' or 'v2.0' etc
        if re.match('v\d+\.?\d*', url_bits[-1]):
            endpoint = '/'.join(url_bits[:-1])
        return endpoint

    def _get_image_endpoint(self):
        """Get an Glance endpoint."""
        endpoint_kwargs = {
            'service_type': 'image',
            'endpoint_type': 'publicURL',
        }
        if 'OS_REGION_NAME' in env:
            endpoint_kwargs['attr'] = 'region'
            endpoint_kwargs['filter_value'] = env['OS_REGION_NAME']
        endpoint = self._get_ksclient().service_catalog.url_for(
                                                    **endpoint_kwargs)
        return self._strip_version(endpoint)

    def _get_network_endpoint(self):
        """Get an Neutron endpoint."""
        endpoint_kwargs = {
            'service_type': 'network',
            'endpoint_type': 'publicURL',
        }
        if 'OS_REGION_NAME' in env:
            endpoint_kwargs['attr'] = 'region'
            endpoint_kwargs['filter_value'] = env['OS_REGION_NAME']
        endpoint = self._get_ksclient().service_catalog.url_for(
                                                    **endpoint_kwargs)
        return endpoint

    def _get_image_client(self):
        """Get a Glance v1 client."""
        if not self._g_client:
            self._g_client = glanceclient.Client('1',
                                    self._get_image_endpoint(),
                                    token=self._get_auth_token())
        return self._g_client

    def _get_compute_client(self):
        """Get a Nova 2 client."""
        if not self._n_client:
            creds = {}
            creds['version'] = '2'
            creds['username'] = env['OS_USERNAME']
            creds['api_key'] = env['OS_PASSWORD']
            creds['auth_url'] = env['OS_AUTH_URL']
            creds['project_id'] = env['OS_TENANT_NAME']
            self._n_client = compclient.Client(**creds)
        return self._n_client

    def _get_volume_client(self):
        """Get Cinder v1 client."""
        if not self._c_client:
            self._c_client = volclient.Client(
                env['OS_USERNAME'],
                env['OS_PASSWORD'],
                env['OS_TENANT_NAME'],
                env['OS_AUTH_URL']
            )
        return self._c_client

    def _get_network_client(self):
        """Get Neutron v2 client"""
        if not self._net_client:
            net_endpoint = self._get_network_endpoint()
            auth_token = self._get_auth_token()
            self._net_client = netclient.Client('2.0',
                                                endpoint_url=net_endpoint,
                                                token=auth_token)
        return self._net_client

    def _getch(self):
        fd = sys.stdin.fileno()
        oldterm = termios.tcgetattr(fd)
        newattr = termios.tcgetattr(fd)
        newattr[3] = newattr[3] & ~termios.ICANON & ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, newattr)
        oldflags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, oldflags | os.O_NONBLOCK)
        try:
            while 1:
                try:
                    c = sys.stdin.read(1)
                    break
                except IOError:
                    pass
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, oldterm)
            fcntl.fcntl(fd, fcntl.F_SETFL, oldflags)
        sys.stdout.write('\n')
        return c

    def _discover_tmos_instances_by_image(self):
        if not self._discovered_tmos_disk_images:
            self._discover_tmos_images()
        nova = self._get_compute_client()
        servers = list(
                       nova.servers.list(
                            detailed=True,
                            search_opts={
                                        'os_vendor': 'f5_networks',
                                        'all_tenants': 1
                                        }
                            )
                       )
        tmos_servers = []
        for server in servers:
            if server.image['id'] in \
               self._discovered_tmos_disk_images.keys():
                tmos_servers.append(server)
        return tmos_servers

    def _discover_tmos_instances_by_os_vendor(self):
        nova = self._get_compute_client()
        servers = list(
                       nova.servers.list(
                           detailed=True,
                           search_opts={
                                        'os_vendor': 'f5_networks',
                                        'all_tenants': 1
                                        }
                        )
        )
        tmos_server = []
        for server in servers:
            if 'os_vendor' in server.metadata and \
               server.metadata['os_vendor'] == 'f5_networks':
                tmos_server.append(server)
        return tmos_server

    def _get_tmos_device_service_groups(self):
        existing_dsg = {}
        servers = self._discover_tmos_instances_by_os_vendor()
        for server in servers:
            if 'f5_device_group' in server.metadata:
                dsg_name = server.metadata['f5_device_group']
                if not dsg_name in existing_dsg:
                    existing_dsg[dsg_name] = []
                existing_dsg[dsg_name].append(server)
        return existing_dsg

    def _get_security_group(self, group_name):
        nova = self._get_compute_client()
        return nova.security_groups.find(name=group_name)

    def _discover_tmos_images(self):
        glance = self._get_image_client()
        filters = {'properties': {'os_vendor': 'f5_networks'}}
        for image in list(glance.images.list(filters=filters)):
            if image.properties['os_type'] == 'f5bigip_datastor':
                self._discovered_tmos_volume_images[image.id] = image
            else:
                self._discovered_tmos_disk_images[image.id] = image
            if 'nova_flavor' in image.properties:
                self._flavor_list.append(image.properties['nova_flavor'])

    def _discover_tmos_flavors(self):
        if not self._discovered_tmos_disk_images:
            self._discover_tmos_images()
        nova = self._get_compute_client()
        for flavor in list(nova.flavors.list()):
            if flavor.name in self._flavor_list:
                self._discovered_tmos_flavors[flavor.id] = flavor

    def _validate_basekey(self, basekey):
        if len(basekey) > 30 and basekey.count('-') > 3:
            return True
        else:
            return False

    def _resolve_disk_image_id(self, image):
        try:
            UUID(image, version=4)
            return image
        except ValueError:
            self._discover_tmos_images()
            for image_obj in self._discovered_tmos_disk_images.values():
                if image_obj.name == image:
                    return image_obj.id
            else:
                return None

    def _resolve_flavor_id(self, flavor):
        try:
            UUID(flavor, version=4)
            return flavor
        except ValueError:
            self._discover_tmos_flavors()
            for flavor_obj in self._discovered_tmos_flavors.values():
                if flavor_obj.name == flavor:
                    return flavor_obj.id
            else:
                return None

    def image_report(self, output_json=False):
        if output_json:
            return self._image_json_report()
        else:
            return self._image_report()

    def _image_report(self):
        self._discover_tmos_images()
        if self._discovered_tmos_disk_images or \
           self._discovered_tmos_volume_images:
            headings = ["ID", "Name", "Flavor", "Type"]
            x = prettytable.PrettyTable(headings)
            for col in headings:
                x.align[col] = 'l'
            di = self._discovered_tmos_disk_images
            for image_id in di:
                image_name = di[image_id].name
                if 'nova_flavor' in di[image_id].properties:
                    image_flavor = di[image_id].properties['nova_flavor']
                else:
                    image_flavor = 'None'
                if 'os_type' in di[image_id].properties:
                    image_type = di[image_id].properties['os_type']
                else:
                    image_type = 'Generic'
                x.add_row([image_id, image_name, image_flavor, image_type])
            vi = self._discovered_tmos_volume_images
            for image_id in vi:
                image_name = vi[image_id].name
                if 'nova_flavor' in vi[image_id].properties:
                    image_flavor = vi[image_id].properties['nova_flavor']
                else:
                    image_flavor = 'None'
                if 'os_type' in vi[image_id].properties:
                    image_type = vi[image_id].properties['os_type']
                else:
                    image_type = 'Generic'
                x.add_row([image_id, image_name, image_flavor, image_type])
            print x

    def _image_json_report(self):
        self._discover_tmos_images()
        if self._discovered_tmos_disk_images or \
           self._discovered_tmos_volume_images:
            di = self._discovered_tmos_disk_images
            disk_images = {}
            for image_id in di:
                image_name = di[image_id].name
                if 'nova_flavor' in di[image_id].properties:
                    image_flavor = di[image_id].properties['nova_flavor']
                else:
                    image_flavor = 'None'
                if 'os_type' in di[image_id].properties:
                    image_type = di[image_id].properties['os_type']
                else:
                    image_type = 'Generic'
                disk_images[image_id] = {}
                disk_images[image_id]['name'] = image_name
                disk_images[image_id]['flavor'] = image_flavor
                disk_images[image_id]['type'] = image_type
            vi = self._discovered_tmos_volume_images
            volume_images = {}
            for image_id in vi:
                image_name = vi[image_id].name
                if 'nova_flavor' in vi[image_id].properties:
                    image_flavor = vi[image_id].properties['nova_flavor']
                else:
                    image_flavor = 'None'
                if 'os_type' in vi[image_id].properties:
                    image_type = vi[image_id].properties['os_type']
                else:
                    image_type = 'Generic'
                volume_images[image_id] = {}
                volume_images[image_id]['name'] = image_name
                volume_images[image_id]['flavor'] = image_flavor
                volume_images[image_id]['type'] = image_type
            images = {'images': {'disk_images': disk_images,
                                 'volume_images': volume_images}}
            print json.dumps(images, indent=4, sort_keys=True)

    def instance_report(self, output_json=False):
        if output_json:
            return self._instance_json_report()
        else:
            return self._instance_report()

    def _instance_report(self):
        printed_servers = []
        image_servers = self._discover_tmos_instances_by_image()
        vendor_servers = self._discover_tmos_instances_by_os_vendor()
        if image_servers or vendor_servers:
            headings = ["ID", "Name", "Device Group",
                        "Flavor", "Image", "Status"]
            x = prettytable.PrettyTable(headings)
            for col in headings:
                x.align[col] = 'l'
            for server in image_servers:
                printed_servers.append(server.id)
                if server.flavor['id'] in self._discovered_tmos_flavors:
                    server_flavor = \
                        self._discovered_tmos_flavors[server.flavor['id']].name
                else:
                    server_flavor = 'Unmanaged'
                if server.image['id'] in self._discovered_tmos_disk_images:
                    server_image = \
                     self._discovered_tmos_disk_images[server.image['id']].name
                else:
                    server_image = 'Unmanaged'
                if 'f5_device_group' in server.metadata:
                    server_dsg = server.metadata['f5_device_group']
                    if 'f5_device_group_primary_device' in server.metadata:
                        if server.metadata[
                               'f5_device_group_primary_device'] == 'true':
                            server_dsg += ' (primary)'
                else:
                    server_dsg = 'None'
                x.add_row([server.id,
                           server.name,
                           server_dsg,
                           server_flavor,
                           server_image,
                           server.status])
            for server in vendor_servers:
                if not server.id in printed_servers:
                    printed_servers.append(server.id)
                    if server.flavor['id'] in self._discovered_tmos_flavors:
                        server_flavor = \
                        self._discovered_tmos_flavors[server.flavor['id']].name
                    else:
                        server_flavor = 'Unmanaged'
                    server_image = 'Unmanaged'
                    if 'f5_device_group' in server.metadata:
                        server_dsg = server.metadata['f5_device_group']
                        if 'f5_device_group_primary_device' in server.metadata:
                            if server.metadata[
                               'f5_device_group_primary_device'] == 'true':
                                server_dsg += ' (primary)'
                    else:
                        server_dsg = 'None'
                    x.add_row([server.id,
                               server.name,
                               server_dsg,
                               server_flavor,
                               server_image,
                               server.status])
            print x

    def _instance_json_report(self):
        printed_servers = []
        image_servers = self._discover_tmos_instances_by_image()
        vendor_servers = self._discover_tmos_instances_by_os_vendor()
        if image_servers or vendor_servers:
            image_server = {}
            for server in image_servers:
                printed_servers.append(server.id)
                if server.flavor['id'] in self._discovered_tmos_flavors:
                    server_flavor = \
                        self._discovered_tmos_flavors[server.flavor['id']].name
                else:
                    server_flavor = 'None'
                if server.image['id'] in self._discovered_tmos_disk_images:
                    server_image = \
                     self._discovered_tmos_disk_images[server.image['id']].name
                else:
                    server_image = 'None'
                if 'f5_device_group' in server.metadata:
                    server_dsg = server.metadata['f5_device_group']
                    if 'f5_device_group_primary_device' in server.metadata:
                        if server.metadata[
                               'f5_device_group_primary_device'] == 'true':
                            server_dsg_primary = True
                else:
                    server_dsg = 'None'
                    server_dsg_primary = False
                image_server[server.id] = {}
                image_server[server.id]['name'] = server.name
                image_server[server.id]['f5_device_service_group'] = server_dsg
                image_server[server.id]['f5_device_service_group_primary'] = \
                                                             server_dsg_primary
                image_server[server.id]['flavor'] = {'id': server.flavor['id'],
                                                     'name': server_flavor}
                image_server[server.id]['image'] = {'id': server.image.id,
                                                    'name': server_image}
                image_server[server.id]['status'] = server.status
            for server in vendor_servers:
                if not server.id in printed_servers:
                    image_server[server.id] = {}
                    if 'f5_device_group' in server.metadata:
                        server_dsg = server.metadata['f5_device_group']
                    if 'f5_device_group_primary_device' in server.metadata:
                        if server.metadata[
                               'f5_device_group_primary_device'] == 'true':
                            server_dsg_primary = True
                    else:
                        server_dsg = 'None'
                        server_dsg_primary = False
                    image_server[server.id]['name'] = server.name
                    image_server[server.id][
                            'f5_device_service_group'] = server_dsg
                    image_server[server.id][
                            'f5_device_service_group_primary'] = \
                                                        server_dsg_primary
                    image_server[server.id]['flavor'] = \
                        {'id': server.flavor['id'], 'name': 'None'}
                    image_server[server.id]['image'] = \
                        {'id': server.image['id'], 'name': 'None'}
                    image_server[server.id]['status'] = server.status
            servers = {'servers': image_server}
            print json.dumps(servers, indent=4, sort_keys=True)

    def build_policy_file(self):
        policies = {}
        policies['devicegroups'] = []
        policy = {}
        policies['devicegroups'].append(policy)

        need_dsg_name = True
        need_type = True
        need_tenant = True
        tenant_id = None
        tenant_name = None
        need_image = True
        need_key = True
        need_sec_group = True
        need_tmos_admin_password = True
        tmos_admin_password = None
        need_tmos_root_password = True
        tmos_root_password = None
        number_of_devices_needed = 0

        license_basekeys = []
        assigned_networks = []

        selected_image = None
        selected_flavor = None

        MAX_VIFS = 10
        need_mgmt_net = True
        mgmt_net = None
        need_ha_net = True
        ha_net = None
        need_vtep_net = False
        vtep_net = None

        indexed_networks = {}

        print "\n\nDevice Service Group Policy Builder\n\n"

        try:
            dsgs = self._get_tmos_device_service_groups()
            while need_dsg_name:
                sys.stdout.write("New Device Service Group Name: ")
                dsg_name = sys.stdin.readline().strip()
                if dsg_name in dsgs:
                    print "WARNING! - %s already exists" % dsg_name
                    sys.stdout.write(
                      "Do you want me to delete the instances in %s? [y/n]: "
                      % dsg_name
                    )
                    del_dsg = strtobool(self._getch())
                    if del_dsg:
                        for server in dsgs[dsg_name]:
                            server.delete()
                        del dsgs[dsg_name]
                else:
                    need_dsg_name = False
                    policy['f5_device_group'] = dsg_name
            if need_type:
                headings = ["No.", "Cluster Type", "Device Count"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                x.add_row([1, "Standalone", 1])
                x.add_row([2, "HA Pair", 2])
                x.add_row([3, "ScaleN", 4])
                print x
            while need_type:
                sys.stdout.write("Cluster Type? (1,2,3): ")
                cluster_type = int(sys.stdin.readline().strip())
                if cluster_type in [1, 2, 3]:
                    if cluster_type == 1:
                        policy['f5_ha_type'] = 'standalone'
                        number_of_devices_needed = 1
                        need_ha_net = False
                    elif cluster_type == 2:
                        policy['f5_ha_type'] = 'hapair'
                        number_of_devices_needed = 2
                    elif cluster_type == 3:
                        policy['f5_ha_type'] = 'scalen'
                        number_of_devices_needed = 4
                    need_type = False
            if need_tenant:
                headings = ["No.", "ID", "Tenant"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                keystone = self._get_ksclient()
                tenants = keystone.tenants.list()
                choices = {}
                tenant_ids = {}
                i = 1
                for tenant in tenants:
                    if not tenant.name == 'service' and tenant.enabled:
                        choices[i] = tenant.name
                        tenant_ids[i] = tenant.id
                        x.add_row([i, tenant.id, tenant.name])
                        i = i + 1
                print x
            while need_tenant:
                sys.stdout.write("Tenant? (1..%d): " % len(choices))
                choice = int(sys.stdin.readline().strip())
                if choice in range(1, len(choices) + 1):
                    policy['tenant'] = choices[choice]
                    tenant_name = choices[choice]
                    tenant_id = tenant_ids[choice]
                    need_tenant = False
            if need_image:
                headings = ["No.", "ID", "Image"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                self._discover_tmos_images()
                choices = {}
                i = 1
                for image in self._discovered_tmos_disk_images.values():
                    if image.is_public or image.owner == tenant_id:
                        choices[i] = image
                        x.add_row([i, image.id, image.name])
                        i = i + 1
                print x
            while need_image:
                sys.stdout.write("Image? (1..%d): " % len(choices))
                choice = int(sys.stdin.readline().strip())
                if choice in range(1, len(choices) + 1):
                    policy['image'] = choices[choice].name
                    selected_image = choices[choice]
                    selected_flavor = \
                        choices[choice].properties['nova_flavor']
                    print "Setting Flavor to %s" % selected_flavor
                    need_image = False
            if need_key:
                headings = ["No.", "Key Name", "Fingerprint"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                x.add_row([0, "None", "None"])
                choices = {}
                choices[0] = 'none'
                i = 1
                nova = self._get_compute_client()
                for key in nova.keypairs.list():
                    choices[i] = key.id
                    x.add_row([i, key.id, key.fingerprint])
                    i = i + 1
                print x
            while need_key:
                sys.stdout.write("SSH Key? (0..%d): " % (len(choices) - 1))
                choice = int(sys.stdin.readline().strip())
                if choice in range(0, len(choices)):
                    policy['key_name'] = choices[choice]
                    need_key = False
            if need_sec_group:
                headings = ["No.", "ID", "Security Group", "Description"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                choices = {}
                i = 1
                nova = self._get_compute_client()
                for sg in nova.security_groups.list(
                                        search_opts={'tenant_id': tenant_id}):
                    choices[i] = sg.id
                    x.add_row([i, sg.id, sg.name, sg.description])
                    i = i + 1
                print x
            while need_sec_group:
                sys.stdout.write("Security Group? (1..%d): " % len(choices))
                choice = int(sys.stdin.readline().strip())
                if choice in range(1, len(choices) + 1):
                    policy['security_group'] = choices[choice]
                    need_sec_group = False
            while need_tmos_admin_password:
                pprompt = lambda: (
                    getpass.getpass('TMOS admin password: '),
                    getpass.getpass('Retype TMOS admin password: '))
                p1, p2 = pprompt()
                while p1 != p2:
                    print('Passwords do not match. Try again')
                    p1, p2 = pprompt()
                tmos_admin_password = p1
                need_tmos_admin_password = False
            while need_tmos_root_password:
                pprompt = lambda: (
                    getpass.getpass('TMOS root password: '),
                    getpass.getpass('Retype TMOS root password: '))
                p1, p2 = pprompt()
                while p1 != p2:
                    print('Passwords do not match. Try again')
                    p1, p2 = pprompt()
                tmos_root_password = p1
                need_tmos_root_password = False
            while len(license_basekeys) < number_of_devices_needed:
                sys.stdout.write("License basekey (%d/%d): " % \
                                 (len(license_basekeys) + 1,
                                  number_of_devices_needed))
                basekey = sys.stdin.readline().strip()
                if self._validate_basekey(basekey):
                    license_basekeys.append(str(basekey))
                else:
                    print "%s is not a valid base key" % basekey
            neutron = self._get_network_client()
            networks = neutron.list_networks(tenant_id=tenant_id)['networks']
            if need_mgmt_net:
                headings = ["No.", "ID", "Network"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                choices = {}
                i = 1
                for net in networks:
                    if not net['id'] in assigned_networks:
                        choices[i] = net
                        x.add_row([i, net['id'], net['name']])
                        i = i + 1
                print x
            while need_mgmt_net:
                sys.stdout.write(
                    "Management Network? (1..%d): " % len(choices))
                choice = int(sys.stdin.readline().strip())
                if choice in range(1, len(choices) + 1):
                    mgmt_net = choices[choice]
                    assigned_networks.append(mgmt_net['id'])
                    need_mgmt_net = False
            if need_ha_net:
                headings = ["No.", "ID", "Network"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                choices = {}
                i = 1
                for net in networks:
                    if not net['id'] in assigned_networks:
                        choices[i] = net
                        x.add_row([i, net['id'], net['name']])
                        i = i + 1
                print x
            while need_ha_net:
                sys.stdout.write("Failover Network? (1..%d): " % len(choices))
                choice = int(sys.stdin.readline().strip())
                if choice in range(1, len(choices) + 1):
                    ha_net = choices[choice]
                    assigned_networks.append(ha_net['id'])
                    need_ha_net = False
            if tenant_name == 'admin':
                sys.stdout.write(
                    "Provide VTEP endpoints for SDN tunnels? [y/n]: ")
                choice = sys.stdin.readline().strip()
                if choice in ['y', 'Y', 'yes', 'YES']:
                    need_vtep_net = True
            else:
                need_vtep_net = False
            if need_vtep_net:
                headings = ["No.", "ID", "Network"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                choices = {}
                i = 1
                for net in networks:
                    if not net['id'] in assigned_networks:
                        choices[i] = net
                        x.add_row([i, net['id'], net['name']])
                        i = i + 1
                print x
            while need_vtep_net:
                sys.stdout.write("VTEP Network? (1..%d): " % len(choices))
                choice = int(sys.stdin.readline().strip())
                if choice in range(1, len(choices) + 1):
                    vtep_net = choices[choice]
                    assigned_networks.append(vtep_net['id'])
                    need_vtep_net = False
            number_availble_vifs = MAX_VIFS - len(assigned_networks)
            for j in range(number_availble_vifs):
                headings = ["No.", "ID", "Network"]
                x = prettytable.PrettyTable(headings)
                for col in headings:
                    x.align[col] = 'l'
                choices = {}
                i = 1
                for net in networks:
                    if not net['id'] in assigned_networks:
                        choices[i] = net
                        x.add_row([i, net['id'], net['name']])
                        i = i + 1
                print x
                sys.stdout.write("Add another network? (1..%d) or N: " \
                                 % len(choices))
                choice = sys.stdin.readline().strip()
                if choice in ['n', 'N', 'no', 'NO']:
                    break
                else:
                    choice = int(choice)
                    if choice in range(1, len(choices) + 1):
                        indexed_networks[j] = choices[choice]
                        assigned_networks.append(indexed_networks[j]['id'])

            subnets = neutron.list_subnets(tenant_id=tenant_id)['subnets']
            target_dg_subnets = []
            for subnet in subnets:
                net_id = subnet['network_id']
                if mgmt_net and net_id == mgmt_net['id']:
                    continue
                if ha_net and net_id == ha_net['id']:
                    continue
                if vtep_net and net_id == vtep_net['id']:
                    target_dg_subnets.append(
                        {'name': 'VTEP Network',
                         'gateway_ip': subnet['gateway_ip'],
                         'ip_version': subnet['ip_version']})
                elif net_id in assigned_networks:
                    target_dg_subnets.append(
                        {'name': subnet['name'],
                         'gateway_ip': subnet['gateway_ip'],
                         'ip_version': subnet['ip_version']})
                for i in indexed_networks:
                    if indexed_networks[i]['id'] == net_id:
                        indexed_networks[i]['subnet_name'] = subnet['name']
            headings = ["No.", "Subnet", "Gateway Address"]
            x = prettytable.PrettyTable(headings)
            for col in headings:
                x.align[col] = 'l'
            choices = {}
            i = 1
            for dg in target_dg_subnets:
                choices[i] = dg
                x.add_row([i, dg['name'], dg['gateway_ip']])
                i = i + 1
            print x
            need_dg = True
            dg_subnet = None
            while need_dg:
                sys.stdout.write(
                    "Which Default Gateway? (1..%d): " % len(choices))
                choice = int(sys.stdin.readline().strip())
                if choice in range(1, len(choices) + 1):
                    dg_subnet = choices[choice]
                    need_dg = False
            policy['bigips'] = []
            for i in range(number_of_devices_needed):
                bigip = {}
                meta = {}
                meta['f5_device_group'] = policy['f5_device_group']
                if i == 0:
                    meta['f5_device_group_primary_device'] = 'true'
                else:
                    meta['f5_device_group_primary_device'] = 'false'
                meta['f5_ha_type'] = policy['f5_ha_type']
                meta['os_vendor'] = selected_image.properties['os_vendor']
                meta['os_version'] = selected_image.properties['os_version']
                meta['os_name'] = selected_image.properties['os_name']
                meta['os_type'] = selected_image.properties['os_type']
                bigip['flavor'] = selected_flavor
                bigip['meta'] = meta
                if 'key_name' in policy:
                    bigip['ssh_key_inject'] = 'true'
                bigip['change_passwords'] = 'true'
                bigip['admin_password'] = tmos_admin_password
                bigip['root_password'] = tmos_root_password
                basekey = license_basekeys.pop()
                bigip['license'] = {'basekey': basekey}
                bigip['network'] = {'dhcp': 'true',
                                    'management_network_id': mgmt_net['id'],
                                    'management_network_name': mgmt_net['name']
                                    }
                dr = {}
                if dg_subnet['ip_version'] == 4:
                    dr['destination'] = '0.0.0.0/0'
                else:
                    dr['destination'] = '::/0'
                dr['gateway'] = dg_subnet['gateway_ip']
                bigip['network']['routes'] = {}
                bigip['network']['routes'] = [dr]
                bigip['network']['interfaces'] = {}
                if ha_net:
                    bigip_ha_net = {}
                    bigip_ha_net['dhcp'] = 'true'
                    bigip_ha_net['vlan_name'] = 'HA'
                    bigip_ha_net['selfip_name'] = 'HA'
                    bigip_ha_net['selfip_allow_service'] = "default"
                    bigip_ha_net['network_id'] = ha_net['id']
                    bigip_ha_net['network_name'] = ha_net['name']
                    bigip_ha_net['is_sync'] = 'true'
                    bigip_ha_net['is_failover'] = 'true'
                    bigip_ha_net['is_mirror_primary'] = 'true'
                    bigip_ha_net['is_mirror_secondary'] = 'false'
                    bigip['network']['interfaces']['1.1'] = bigip_ha_net
                if vtep_net:
                    bigip_vtep_net = {}
                    bigip_vtep_net['dhcp'] = 'true'
                    bigip_vtep_net['vlan_name'] = 'VTEP'
                    bigip_vtep_net['selfip_name'] = 'VTEP'
                    bigip_vtep_net['selfip_allow_service'] = "all"
                    bigip_vtep_net['network_id'] = vtep_net['id']
                    bigip_vtep_net['network_name'] = vtep_net['name']
                    bigip_vtep_net['is_sync'] = 'false'
                    bigip_vtep_net['is_failover'] = 'false'
                    bigip_vtep_net['is_mirror_primary'] = 'false'
                    bigip_vtep_net['is_mirror_secondary'] = 'false'
                    bigip['network']['interfaces']['1.2'] = bigip_vtep_net
                for i in indexed_networks:
                    if_index = '1.'
                    for j in [1, 2, 3, 4, 5, 6, 7, 8, 9]:
                        if "%s%s" % (if_index, j) in bigip['network']:
                            continue
                        if_sub = j
                        break
                    interface = "%s%s" % (if_index, if_sub)
                    net = {}
                    net['dhcp'] = 'true'
                    net['vlan_name'] = "vlan_%s" % indexed_networks[i]['name']
                    net['vlan_name'] = net['vlan_name'][0:15]
                    if 'subnet_name' in indexed_networks[i]:
                        net['selfip_name'] = indexed_networks[i]['subnet_name']
                    else:
                        net['selfip_name'] = "selfip_%s" % \
                                                    indexed_networks[i]['name']
                    net['selfip_allow_service'] = "default"
                    net['network_id'] = indexed_networks[i]['id']
                    net['network_name'] = indexed_networks[i]['name']
                    net['is_sync'] = 'false'
                    net['is_failover'] = 'false'
                    net['is_mirror_primary'] = 'false'
                    net['is_mirror_secondary'] = 'false'
                    bigip['network']['interfaces'][interface] = net
                policy['bigips'].append(bigip)

            output_json = json.dumps(policies, indent=4)
            file_name = "%s_cluster_policy.json" % policy['f5_device_group']
            sys.stdout.write("Output File [%s]: " % file_name)
            user_file_name = sys.stdin.readline().strip()
            if len(user_file_name) == 0:
                user_file_name = file_name
            if os.path.isfile(user_file_name):
                sys.stdout.write("File exists.. Overwrite? [y/n] : ")
                overwrite = strtobool(self._getch())
                if overwrite:
                    os.unlink(user_file_name)
                else:
                    for i in range(1000):
                        user_file_name = "%s._%d" % (user_file_name, i)
                        if not os.path.isfile(user_file_name):
                            break
            fd = open(user_file_name, 'w')
            fd.write(output_json)
            fd.close()
            print "Policy file %s written." % user_file_name
        except KeyboardInterrupt:
            print "\nPolicy built aborted by user input..exiting\n"
            sys.exit(0)
        except SystemExit:
            print "\nPolicy built aborted by process termination..exiting\n"
            sys.exit(0)
        sys.stdout.write("Build cluster from this policy now? [y/n]: ")
        build_dsg = strtobool(self._getch())
        if build_dsg:
            self.build_cluster(user_file_name)

    def build_cluster(self, policy_file):
        fd = open(policy_file, 'r')
        json_data = fd.read()
        fd.close()
        policies = json.loads(json_data)

        for policy in policies['devicegroups']:
            inst_index = 1
            management_network_name = None
            icontrol_username = 'admin'
            icontrol_password = None

            for instance in policy['bigips']:
                guest_name = "%s_%d" % (policy['f5_device_group'],
                                        inst_index)
                if not icontrol_password:
                    icontrol_password = instance['admin_password']
                inst_index = inst_index + 1
                interfaces = instance['network']['interfaces']
                nic_dict = {}
                nic_dict[0] = instance['network']['management_network_id']
                if not management_network_name:
                    management_network_name = \
                            instance['network']['management_network_name']
                for i in [1, 2, 3, 4, 5, 6, 7, 8, 9]:
                    interface_id = "1.%d" % i
                    if interface_id in interfaces:
                        nic_dict[i] = interfaces[interface_id]['network_id']
                nics = [None] * len(nic_dict)
                for i in range(0, len(nic_dict)):
                    nics[i] = {'net-id': nic_dict[i]}
                userdata = {}
                userdata['bigip'] = instance.copy()
                del userdata['bigip']['meta']
                del userdata['bigip']['flavor']
                del userdata['bigip']['network']['management_network_id']
                userdata = json.dumps(userdata)
                create_args = {}
                create_args['name'] = guest_name
                create_args['image'] = \
                    self._resolve_disk_image_id(policy['image'])
                create_args['flavor'] = \
                    self._resolve_flavor_id(instance['flavor'])
                create_args['meta'] = instance['meta']
                create_args['security_groups'] = \
                    [policy['security_group']]
                create_args['userdata'] = userdata
                create_args['key_name'] = policy['key_name']
                create_args['nics'] = nics

                for server in self._discover_tmos_instances_by_os_vendor():
                    if server.name == guest_name:
                        print "Instance named %s exists. Not creating."\
                               % guest_name
                        if not server.status == 'ACTIVE':
                            print "Server is %s. Rebooting." % server.status
                            server.reboot(reboot_type='REBOOT_HARD')
                        break
                else:
                    print "Creating instance %s" % guest_name
                    nova = self._get_compute_client()
                    nova.servers.create(**create_args)

            primary_icontrol_host = None
            primary_device_name = None
            device_names = {}

            while len(device_names) + 1 < len(policy['bigips']):
                bigip_instances = self._discover_tmos_instances_by_os_vendor()
                for bi in bigip_instances:
                    if 'f5_device_group' in bi.metadata and \
                       bi.metadata['f5_device_group'] == \
                       policy['f5_device_group']:
                        if 'f5_device_group_primary_device' in bi.metadata:
                            if bi.metadata[
                                 'f5_device_group_primary_device'] == 'true':
                                primary_device_name = bi.name
                        networks = bi.networks
                        if management_network_name in networks:
                            ip_addresses = networks[management_network_name]
                            device_names[bi.name] = ip_addresses[0]
                            if primary_device_name == bi.name:
                                primary_icontrol_host = ip_addresses[0]
                print "Waiting IP addresses allocations"
                time.sleep(5)

            not_connected = True
            while not_connected:
                print "Attempting to connect to primary host %s:%d" % \
                      (primary_icontrol_host, 443)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                tls_open = sock.connect_ex((primary_icontrol_host, 443))
                if tls_open == 0:
                    not_connected = False
                else:
                    time.sleep(5)
            icontrol_not_available = True
            start_time = time.time()
            icontrol_available_states = ['active', 'standby']
            while icontrol_not_available:
                print "Waiting for TMOS control plane to be available."
                try:
                    primary_bigip = bigip.BigIP(primary_icontrol_host,
                                                icontrol_username,
                                                icontrol_password)
                    primary_bigip.set_timeout(5)
                    state = primary_bigip.device.get_failover_state()
                    if state in icontrol_available_states:
                        icontrol_not_available = False
                    else:
                        print "   iControl connected, but state is %s" % state
                except AttributeError:
                    print "   no APIs available.. likely not licensed yet."
                except Exception as e:
                    print "   attempt failed with %s: %s" % \
                                                     (e.__class__, e.message)
                if time.time() - start_time > 600:
                    print 'Giving up after 10 minutes. Create manually.'
                    print 'Launched management endpoints:'
                    for host in device_names.values():
                        print '    https://%s:443' % host
                    sys.exit(1)
                else:
                    time.sleep(10)

            try:
                for dn in device_names:
                    host = device_names[dn]
                    host_bigip = bigip.BigIP(host,
                                             icontrol_username,
                                             icontrol_password)
                    print "Resetting %s device name to %s" \
                           % (host, dn)
                    host_bigip.cluster.remove_all_devices(
                                name=policy['f5_device_group'])
                    host_bigip.device.reset_trust(dn)
            except Exception as e:
                print "Error resetting device name and trust on %s : %s" % \
                      (host, e.message)
                sys.exit(1)

            if True:
            #try:
                primary_bigip = bigip.BigIP(primary_icontrol_host,
                                                icontrol_username,
                                                icontrol_password)
                primary_bigip.set_timeout(5)
                for dn in device_names:
                    if not dn == primary_device_name:
                        print "Adding %s as a peer to %s" \
                               % (dn, primary_device_name)
                        host = device_names[dn]
                        primary_bigip.cluster.add_peer(dn,
                                                       host,
                                                       icontrol_username,
                                                       icontrol_password)
            #except Exception as e:
            #    print "Error adding devices to %s: %s" % \
            #    (primary_icontrol_host, e.message)
            #    raise e
            #    sys.exit(1)

            try:
                print "Creating device service group %s" \
                      % policy['f5_device_group']
                primary_bigip.cluster.create(policy['f5_device_group'], False)
                print "Adding devices to service group"
                primary_bigip.cluster.add_devices(policy['f5_device_group'],
                                                  device_names.keys())
                time.sleep(f5const.PEER_ADD_ATTEMPT_DELAY)
            except Exception as e:
                print "Error device group creation. %s : %s" % \
                                                   (e.__class__, e.message)
                sys.exit(1)

            try:
                print "Syncing group %s" % policy['f5_device_group']
                primary_bigip.cluster.sync(policy['f5_device_group'])
            except Exception as e:
                print "Error device groip creation. %s : %s" % \
                                                   (e.__class__, e.message)
                sys.exit(1)


def main():
    if len(sys.argv) == 1:
        sys.argv.append("--h")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-i', '--listimages',
        action="store_true",
        help='List f5 images.'
    )
    parser.add_argument(
        '-g', '--listinstances',
        action="store_true",
        help='List f5 guest instances.'
    )
    parser.add_argument(
        '-b', '--buildpolicyfile',
        action="store_true",
        help='Build a cluster policy file.'
    )
    parser.add_argument(
        '-p', '--clusterpolicyfile',
        default=None,
        help='Build a cluster from a policy file.'
    )
    parser.add_argument(
        '-j', '--json',
        action="store_true",
        help='Report with json format.'
    )
    args = parser.parse_args()
    clusterpolicyfile = args.clusterpolicyfile
    buildpolicyfile = False
    if args.buildpolicyfile:
        buildpolicyfile = True
    listimages = False
    if args.listimages:
        listimages = True
    listinstances = False
    if args.listinstances:
        listinstances = True
    jsonformat = False
    if args.json:
        jsonformat = True

    manager = F5Manager()
    if listimages:
        manager.image_report(jsonformat)
    if listinstances:
        manager.instance_report(jsonformat)
    if buildpolicyfile:
        manager.build_policy_file()
        sys.exit(0)
    if clusterpolicyfile:
        manager.build_cluster(clusterpolicyfile)


if __name__ == '__main__':
    main()
