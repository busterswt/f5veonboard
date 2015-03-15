#!/usr/bin/env python

import os
import sys
import json
import glanceclient
import re
import argparse
import termios  # @UnresolvedImport
import fcntl
import shutil

from os import environ as env
from distutils.util import strtobool
from keystoneclient.v2_0 import client as ksclient
from novaclient import client as nclient
from cinderclient.v1 import client as cclient


class ImageSync():

    _ks_client = None
    _g_client = None
    _n_client = None
    _c_client = None
    _auth_token = None
    _tmos_image_tool = None
    _image_dir = None

    def __init__(self):
        self._get_image_dir()

    def _setup(self, tempdirectory):
        pass

    def _tear_down(self, tempdirectory):
        pass

    def _get_ksclient(self):
        """Get Keystone Client."""
        if not self._ks_client:
            self._ks_client = ksclient.Client(
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
            self._n_client = nclient.Client(**creds)
        return self._n_client

    def _get_volume_client(self):
        """Get Cinder v1 client."""
        if not self._c_client:
            self._c_client = cclient.Client(
                env['OS_USERNAME'],
                env['OS_PASSWORD'],
                env['OS_TENANT_NAME'],
                env['OS_AUTH_URL']
            )
        return self._c_client

    def _find_tmos_openstack_image_tool(self):
        self._tmos_image_tool = None
        script_path = os.path.dirname(os.path.realpath(__file__))
        cwd = os.getcwd()
        if os.path.isfile("%s/tmos_openstack_image" % cwd):
            self._tmos_image_tool = "%s/tmos_openstack_image" % cwd
            return self._tmos_image_tool
        if os.path.isfile("%s/utils/tmos_openstack_image" % cwd):
            self._tmos_image_tool = "%s/utils/tmos_openstack_image" % cwd
            return self._tmos_image_tool
        if os.path.isfile("%s/tmos_openstack_image" % script_path):
            self._tmos_image_tool = "%s/tmos_openstack_image" % script_path
            return self._tmos_image_tool
        if os.path.isfile("%s/utils/tmos_openstack_image" % script_path):
            self._tmos_image_tool = \
                "%s/utils/tmos_openstack_image" % script_path
            return self._tmos_image_tool
        return None

    def _get_image_dir(self):
        self._image_dir = None
        script_path = os.path.dirname(os.path.realpath(__file__))
        cwd = os.getcwd()
        if os.path.isdir("%s/images" % cwd):
            self._image_dir = "%s/images" % cwd
            return self._image_dir
        if os.path.isdir("%s/images" % script_path):
            self._image_dir = "%s/images" % script_path
            return self._image_dir
        os.mkdir("%s/images" % cwd)
        self._image_dir = "%s/images" % cwd
        return self._image_dir

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

    def _download_f5_images(self, f5_image):
        for url in f5_image['urls']:
            cmd = "wget --quiet -t 10 -c -N -P %s --content-disposition %s" \
                  % (self._image_dir, url)
            print "Downloading %s" % url
            os.system(cmd)
        have_all_files = True
        if not os.path.isfile(
                "%s/%s" % (self._image_dir, f5_image['container_file_name'])):
            have_all_files = False
        if not f5_image['base_iso_file'] == 'none':
            if not os.path.isfile(
                "%s/%s" % (self._image_dir, f5_image['base_iso_file'])):
                have_all_files = False
        if not f5_image['hf_iso_file'] == 'none':
            if not os.path.isfile(
                "%s/%s" % (self._image_dir, f5_image['hf_iso_file'])):
                have_all_files = False
        if have_all_files:
            return True
        else:
            return False

    def _extract_disk_images(self, f5_image, target_directory=None):
        if not target_directory:
            target_directory = self._image_dir
        if os.path.isfile("%s/%s" % (self._image_dir,
                                     f5_image['container_file_name'])):
            if f5_image['container_file_name'].endswith('.zip'):
                uzcmd = "unzip -o -d %s %s %s" % (
                        target_directory,
                        "%s/%s" % (self._image_dir,
                                   f5_image['container_file_name']),
                        f5_image['disk_image_file']
                )
                os.system(uzcmd)
                for volume in f5_image['volumes']:
                    for image_file in volume.keys():
                        volume_file = volume[image_file]['volume_file']
                        uzcmd = "unzip -o -d %s %s %s" % (
                                target_directory,
                                "%s/%s" % (self._image_dir,
                                           f5_image['container_file_name']),
                                volume_file
                        )
                        os.system(uzcmd)
        if not f5_image['base_iso_file'] == 'none':
            if not os.path.isfile("%s/%s" % (target_directory,
                                             f5_image['base_iso_file'])):
                if os.path.isfile("%s/%s" % (self._image_dir,
                                             f5_image['base_iso_file'])):
                    shutil.copy2("%s/%s" % (self._image_dir,
                                            f5_image['base_iso_file']),
                                 "%s/%s" % (target_directory,
                                            f5_image['base_iso_file']))
        if not f5_image['hf_iso_file'] == 'none':
            if not os.path.isfile("%s/%s" % (target_directory,
                                             f5_image['hf_iso_file'])):
                if os.path.isfile("%s/%s" % (self._image_dir,
                                             f5_image['hf_iso_file'])):
                    shutil.copy2("%s/%s" % (self._image_dir,
                                            f5_image['hf_iso_file']),
                                 "%s/%s" % (target_directory,
                                            f5_image['hf_iso_file']))

    def _create_disk_image(self, f5_image, target_directory=None):
        if not target_directory:
            target_directory = self._image_dir
        create_image = True
        if not self._find_tmos_openstack_image_tool():
            print "Can not find the tmos_openstack_image utility."
            create_image = False
        startup_script = self._find_startup_agent_script(
                                                  f5_image['startup_script'])
        if not startup_script:
            print "No startup agent script %s found." % startup_script
            create_image = False
        userdata_file = self._find_userdata_file(f5_image['default_userdata'])
        if not userdata_file:
            print "No default user data: %s found." % userdata_file
            create_image = False
        image_file = '%s/%s' % (target_directory, f5_image['disk_image_file'])
        if not os.path.isfile(image_file):
            print "Disk file image %s not found." % image_file
            create_image = False
        base_iso_file = None
        if not f5_image['base_iso_file'] == 'none':
            base_iso_file = "%s/%s" % (target_directory,
                                                    f5_image['base_iso_file'])
            if not os.path.isfile(base_iso_file):
                print "Base TMOS ISO file %s not found." % base_iso_file
                create_image = False
        hf_iso_file = None
        if not f5_image['hf_iso_file'] == 'none':
            hf_iso_file = "%s/%s" % (target_directory, f5_image['hf_iso_file'])
            if not os.path.isfile(hf_iso_file):
                print "Base TMOS HF ISO file %s not found." % hf_iso_file
                create_image = False
        if not create_image:
            print "Can not create image. Requirements not met."
            return False
        create_image_cmd = "sudo %s " % self._tmos_image_tool
        if f5_image['firstboot_flag_file'] == 'true':
            create_image_cmd += '-f '
        create_image_cmd += "-o \"%s\" " % f5_image['image_name']
        create_image_cmd += "-s \"%s\" " % startup_script
        create_image_cmd += "-u \"%s\" " % userdata_file
        if hf_iso_file and base_iso_file:
            create_image_cmd += "-b \"%s\" -h \"%s\" " % (base_iso_file,
                                                          hf_iso_file)
        create_image_cmd += "-x \"%s\" " % target_directory
        create_image_cmd += "-w \"%s\" " % target_directory
        create_image_cmd += " \"%s\" " % image_file
        print "Issuing Command: %s" % create_image_cmd
        os.system(create_image_cmd)
        properties = {}
        properties['os_name'] = f5_image['os_name']
        properties['os_type'] = f5_image['os_type']
        properties['os_vendor'] = f5_image['os_vendor']
        properties['os_version'] = f5_image['os_version']
        properties['nova_flavor'] = f5_image['flavor']
        properties['description'] = f5_image['image_description']
        glance = self._get_image_client()
        new_image = glance.images.create(
            name=f5_image['image_name'],
            disk_format=f5_image['disk_format'],
            container_format=f5_image['container_format'],
            min_disk=f5_image['min-disk'],
            min_ram=f5_image['min-ram'],
            properties=properties,
            is_public='true'
        )
        print "Glance image %s with id %s created" % (new_image.name,
                                                      new_image.id)
        new_image_file = "%s/%s" % (target_directory,
                                    f5_image['image_name'])
        print "Uploading %s to image %s" % (new_image_file, new_image.id)
        new_image.update(data=open(new_image_file, 'rb'))
        print "Removing temporary build image %s" % new_image_file
        os.unlink(new_image_file)

    def _create_volume_type(self):
        cinder = self._get_volume_client()
        for vt in cinder.volume_types.list():
            if vt.name == 'F5.DATASTOR':
                break
        else:
            vt = cinder.volume_types.create('F5.DATASTOR')
            vt.set_keys({'type': 'datastor'})
            vt.set_keys({'vendor': 'f5_networks'})

    def _create_volumes(self, f5_image, target_directory=None):
        if 'volumes' in f5_image and f5_image['volumes']:
            glance = self._get_image_client()
            glance_images = glance.images.list()
            for volume in f5_image['volumes']:
                for vi_name in volume.keys():
                    for image in glance_images:
                        if image.name == vi_name:
                            break
                    else:
                        volume_file = volume[vi_name]['volume_file']
                        if os.path.isfile("%s/%s" % (target_directory,
                                                     volume_file)):
                            properties = {}
                            properties['os_name'] = f5_image['os_name']
                            properties['os_type'] = 'f5bigip_datastor'
                            properties['os_vendor'] = f5_image['os_vendor']
                            properties['os_version'] = f5_image['os_version']
                            properties['description'] = \
                                   'DATASTOR image for %s' % f5_image['name']
                            f5vi = volume[vi_name]
                            new_image = \
                                glance.images.create(
                                  name=vi_name,
                                  disk_format=f5vi['disk_format'],
                                  container_format=f5vi['container_format'],
                                  min_disk=f5vi['min-disk'],
                                  properties=properties,
                                  is_public='true'
                                )
                            print "Glance image %s with id %s created" % (
                                                               new_image.name,
                                                               new_image.id)
                            new_image_file = "%s/%s" % (target_directory,
                                                        volume_file)
                            print "Uploading %s to image %s" % (new_image_file,
                                                                new_image.id)
                            new_image.update(data=open(new_image_file, 'rb'))

    def _create_flavor(self, f5_image):
        nova = self._get_compute_client()
        for flavor in nova.flavors.list():
            if f5_image['flavor'] == flavor.name:
                break
        else:
            nova.flavors.create(
                name=f5_image['flavor'],
                vcpus=f5_image['vcpus'],
                ram=f5_image['min-ram'],
                disk=f5_image['min-disk'],
                is_public=True
            )

    def _find_startup_agent_script(self, startupfile):
        if os.path.isfile(startupfile):
            return startupfile
        script_path = os.path.dirname(os.path.realpath(__file__))
        cwd = os.getcwd()
        if os.path.isfile("%s/agents/%s" % (cwd, startupfile)):
            return "%s/agents/%s" % (cwd, startupfile)
        if os.path.isfile("%s/agents/%s" % (script_path, startupfile)):
            return "%s/agents/%s" % (script_path, startupfile)
        return None

    def _find_userdata_file(self, userdatafile):
        if os.path.isfile(userdatafile):
            return userdatafile
        script_path = os.path.dirname(os.path.realpath(__file__))
        cwd = os.getcwd()
        if os.path.isfile("%s/includes/%s" % (cwd, userdatafile)):
            return "%s/includes/%s" % (cwd, userdatafile)
        if os.path.isfile("%s/includes/%s" % (script_path, userdatafile)):
            return "%s/includes/%s" % (script_path, userdatafile)
        return None

    def _find_bookmark_file(self, bookmarkfile):
        if os.path.isfile(bookmarkfile):
            return bookmarkfile
        script_path = os.path.dirname(os.path.realpath(__file__))
        cwd = os.getcwd()
        if os.path.isfile("%s/includes/%s" % (cwd, bookmarkfile)):
            return "%s/includes/%s" % (cwd, bookmarkfile)
        if os.path.isfile("%s/includes/%s" % (script_path, bookmarkfile)):
            return "%s/includes/%" % (script_path, bookmarkfile)
        return None

    def download_from_bookmarks(self,
                                bookmark_file,
                                interactive=False):
        if bookmark_file and os.path.isfile(bookmark_file):
            print "Opening %s" % bookmark_file
            bookmark_data = open(bookmark_file)
            bookmarks = ""
            try:
                bookmarks = json.loads(bookmark_data.read())
            except:
                print "Can not parse JSON file %s" % bookmark_file
            bookmark_data.close()
            f5_images = bookmarks['bookmarks']
            for f5_image in f5_images:
                if interactive:
                    sys.stdout.write(
                        "Download %s Containers? [y/n]: "
                        % f5_image['name'])
                    download_image = strtobool(self._getch())
                if download_image:
                    self._download_f5_images(f5_image)

    def sync_from_bookmarks(self,
                            bookmark_file,
                            tempdirectory='/tmp',
                            interactive=False,
                            removefromglance=False):
        if bookmark_file and os.path.isfile(bookmark_file):
            print "Opening %s" % bookmark_file
            bookmark_data = open(bookmark_file)
            bookmarks = ""
            try:
                bookmarks = json.loads(bookmark_data.read())
            except:
                print "Can not parse JSON file %s" % bookmark_file
            bookmark_data.close()
            f5_images = bookmarks['bookmarks']
            f5_disk_images_to_add = []
            f5_volume_images_to_add = []
            f5_images_to_remove = []
            glance = self._get_image_client()
            images = list(glance.images.list())
            existing_image_names = []
            bookmark_image_names = []
            volume_image_names = []
            for image in images:
                existing_image_names.append(image.name)
            for f5_image in f5_images:
                bookmark_image_names.append(f5_image['image_name'])
                if f5_image['volumes']:
                    for vi in f5_image['volumes']:
                        for image_name in vi.keys():
                            volume_image_names.append(image_name)
                            if image_name not in existing_image_names:
                                f5_volume_images_to_add.append(image_name)
                if not f5_image['image_name'] in existing_image_names:
                    f5_disk_images_to_add.append(f5_image)
            if removefromglance:
                for image in images:
                    if 'os_vendor' in image.properties and \
                       image.properties['os_vendor'] == 'f5_networks':
                        if image.name not in bookmark_image_names and \
                           image.name not in volume_image_names:
                            f5_images_to_remove.append(image)
                for image in f5_images_to_remove:
                    remove_image = True
                    if interactive:
                        sys.stdout.write(
                            "Remove %s Glance Image? [y/n]: "
                            % image.name)
                        remove_image = strtobool(self._getch())
                    if remove_image:
                        try:
                            glance.images.delete(image.id)
                        except:
                            print('Could not delete %s Glance image.'
                                  % image.name)
            # base sync setup
            self._setup(tempdirectory)
            # always check that datastor volume type created
            self._create_volume_type()
            for f5_image in f5_disk_images_to_add:
                add_image = True
                if interactive:
                    sys.stdout.write(
                        "Add %s Glance Image? [y/n]: "
                        % f5_image['name'])
                    add_image = strtobool(self._getch())
                if add_image:
                    if self._download_f5_images(f5_image):
                        self._extract_disk_images(f5_image, tempdirectory)
                        self._create_disk_image(f5_image, tempdirectory)
                        self._create_flavor(f5_image)
                        if f5_image['volumes']:
                            for vi in f5_image['volumes']:
                                for image_name in vi.keys():
                                    if image_name in f5_volume_images_to_add:
                                        self._create_volumes(f5_image,
                                                             tempdirectory)
                                        f5_volume_images_to_add.remove(
                                                                  image_name)
            for vi_image in f5_volume_images_to_add:
                for f5_image in f5_images:
                    if f5_image['volumes']:
                        for vi in f5_image['volumes']:
                            for image_name in vi.keys():
                                if image_name == vi_image:
                                    add_image = True
                                    if interactive:
                                        sys.stdout.write(
                                            "Add %s Glance Image? [y/n]: "
                                            % image_name)
                                        add_image = strtobool(self._getch())
                                    if add_image:
                                        self._extract_disk_images(
                                            f5_image,
                                            tempdirectory
                                        )
                                        self._create_volumes(
                                            f5_image,
                                            tempdirectory
                                        )
            # base sync tear_down
            self._tear_down(tempdirectory)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-b', '--bookmarkfile',
        help='JSON file containing TMOS Virtual Edition references.'
    )
    parser.add_argument(
        '-t', '--tempdirectory',
        default='/tmp',
        help='Directory to extract image files from their containers.'
    )
    parser.add_argument(
        '-r', '--removefromglance',
        action="store_true",
        help='Remove images from Glance which are not in the bookmarks file.'
    )
    parser.add_argument(
        '-i', '--interactive',
        action="store_true",
        help='Get synchronization authorization from CLI'
    )
    parser.add_argument(
        '-d', '--downloadonly',
        action="store_true",
        help='Only download bookmark container files, do not synchronize.'
    )
    args = parser.parse_args()
    bookmarkfile = args.bookmarkfile
    tempdirectory = args.tempdirectory
    interactive = False
    if args.interactive:
        interactive = True
    removefromglance = False
    if args.removefromglance:
        removefromglance = True
    downloadonly = False
    if args.downloadonly:
        downloadonly = True

    image_sync_client = ImageSync()
    if not bookmarkfile:
        bookmarkfile = 'bookmarks.json'
    found_bookmark_file = image_sync_client._find_bookmark_file(bookmarkfile)
    if not found_bookmark_file:
        print "Can not find bookmarkfile %s. Exiting." % bookmarkfile
        sys.exit(1)
    if downloadonly:
        image_sync_client.download_from_bookmarks(found_bookmark_file,
                                                  interactive)
    else:
        image_sync_client.sync_from_bookmarks(found_bookmark_file,
                                              tempdirectory,
                                              interactive,
                                              removefromglance)


if __name__ == '__main__':
    main()
