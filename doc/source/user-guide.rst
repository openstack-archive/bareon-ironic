User Guide
==========

The Bareon Ironic driver is controlled by a JSON file specified using the
deploy_config parameter. This file has a number of sections that control
various deployment stages. These sections and their effect are described below.

The deploy config file contains multiple attributes on the 1st level.

Currently supported attributes are:

::

	{
		"partitions": ...
		"partitions_policy": ...
		"image_deploy_flags": ...
	}

This JSON file is distributed via the deploy configuration image. The reference to
the image can be specified in multiple ways:

* in */etc/ironic/ironic.conf* (mandatory, Cloud Default Configuration Image)
* passed to nova boot (optional)
* linked to the tenant image (optional)
* linked to ironic node (optional).

.. _ironic_bareon_cloud_default_config:

Creating A Bareon Cloud Default Configuration Image
---------------------------------------------------
To use the Ironic bareon driver you must create a Cloud Default Configuration JSON
file in the resource storage. The driver will automatically
refer to it, using the URL "cloud_default_deploy_config". For example:

.. code-block:: console

   root@example # cat cloud_default_deploy_config
    {
        "image_deploy_flags": {
            "rsync_flags": "-a -A -X --timeout 300"
        }
    }

.. warning:: Since an error in the JSON file will prevent the deploy from
   succeeding, you may want to validate the JSON syntax, for example by
   executing `python -m json.tool < cloud_default_deploy_config`.

To use default resource storage (Glance) create a Glance image using the JSON
file as input.

.. code-block:: console

   root@example # glance image-create --visibility public --disk-format raw \
   --container-format bare --name cloud_default_deploy_config \
   --file cloud_default_deploy_config

See :ref:`url_resolution` for information on how to use alternate storage sources.

Ironic will automatically refer to this image by the URL
*cloud_default_deploy_config* and use it for all deployments. Thus it is highly
recommended to not put any node-specific or image-specific details into the
cloud default config. Attributes in this config can be overridden according
to the priorities in */etc/ironic/ironic.conf*.

.. _ironic_bareon_deploy_config:

Creating A Bareon Deploy Configuration JSON
-------------------------------------------

To use the Ironic bareon driver you must create a deploy configuration JSON file
in the resource storage to reference during the deploy process.

This configuration file should define the desired disk partitioning scheme for
the Ironic nodes. For example:

.. code-block:: console

   root@example # cat deploy_config_example
   {
       "partitions_policy": "clean",
       "partitions": [
           {
               "type": "disk",
               "id": {
                   "type": "name",
                   "value": "sda"
               },
               "size": "10000 MiB",
               "volumes": [
                   {
                       "type": "partition",
                       "mount": "/",
                       "file_system": "ext4",
                       "size": "4976 MiB"
                   },
                   {
                       "type": "partition",
                       "mount": "/opt",
                       "file_system": "ext4",
                       "size": "2000 MiB"
                   },
                   {
                       "type": "pv",
                       "size": "3000 MiB",
                       "vg": "home"
                   }
               ]
           },
           {
               "type": "vg",
               "id": "home",
               "volumes": [
                   {
                       "type": "lv",
                       "name": "home",
                       "mount": "/home",
                       "size": "2936 MiB",
                       "file_system": "ext3"
                   }
               ]
           }
       ]
   }

The JSON structure is explained in the next section.

Refer to :ref:`implicitly_taken_space` for explanation of uneven size values.

To use default resource storage (Glance) create a Glance image using the
JSON deploy configuration file as input.

.. warning:: Since an error in the JSON file will prevent the deploy from
   succeeding, you may want to validate the JSON syntax, for example by
   executing `python -m json.tool < deploy_config_example`.

.. code-block:: console

   root@example # glance image-create --visibility public --disk-format raw \
   --container-format bare --name deploy_config_example \
   --file deploy_config_example

See :ref:`url_resolution` for information on how to use alternate storage sources.

Then the Nova metadata must include a reference to the desired deploy configuration
image, in this example ``deploy_config=deploy_config_example``.  This may be
specified as part of the Nova boot command or as OS::Nova::Server metadata in a Heat
template. An example of the former:

.. code-block:: console

   root@example # nova boot --nic net-id=23c11dbb-421e-44ca-b303-41656a4e6344 \
   --image centos-7.1.1503.raw --flavor ironic_flavor \
   --meta deploy_config=deploy_config_example --key-name=default bareon_test

.. _ironic_bareon_deploy_config_structure:

Deploy Configuration JSON Structure
-----------------------------------

partitions_policy
^^^^^^^^^^^^^^^^^

Defines the partitioning behavior of the driver. Optional, default is "verify".
General structure is:

::

    "partitions_policy": "verify"


The partitions policy can take one of the following values:

**verify** - Applied in two steps:

1. Do verification. Compare partitions schema with existing partitions on the
   disk(s). If the schema matches the on-disk partition layout
   (including registered fstab mount points) then deployment succeeds.
   If the schema does not match the on-disk layout, deployment fails and the
   node is returned to the pool. No modification to the on-disk content is
   made in this case. Any disks present on the target node that are not
   mentioned in the schema are ignored.

.. note:: File */etc/fstab* must be present on the node, and written
   using partition UUIDs. bareon tries to find it on the 1st disk with
   bootloader present, on the 1st primary/logical partition (skipping ones
   marked as bios_grub).

.. note:: LVM verification is not supported currently. PVs/VGs/LVs are not being
   read from the node.

2. Clean data on filesystems marked as keep_data=False. See partitions
   sections below.

**clean** - Applied in a single step:

1. Ignore existing partitions on the disk(s). Clean the disk and create
   partitions according to the schema. Any disks present on the target node
   that are not mentioned in the schema are ignored.

.. _partitions:

partitions
^^^^^^^^^^

An attribute called partitions holds a partitioning schema being applied
to the node during deployment. Required.

General structure and schema flow is:

::

	"partitions": [
		{
			"type": "disk",
			...
			"volumes": [
				{
					"type": "partition",
					...
				},
				...,
				{
					"type": "pv",
					...
				},
				...
			]
		},
		{
			"type": "vg",
			...
			"volumes": [
				{
					"type": "lv",
					...
				},
				...
			]
		},
	]

.. _partitions_disk:

disk
""""

- type - "disk". Required.
- id - Used to find a device. Required. For example:

	::

		"id":{"type": "scsi", "value": "6:1:0:0"}

		"id":{"type": "path",
			  "value" : "disk/by-path/pci-0000:00:07.0-virtio-pci-virtio3"}

		"id":{"type": "name", "value": "vda"}


- size - Size of disk. Required.
- volumes - Array of partitions / physical volumes. See below. Required.

.. note:: All "size" values are strings containing either an integer number
   and size unit (e.g., "100 MiB" or 100MiB"). In the case of partitions a
   value relative to the size of the disk (e.g., "40%") may also be used.

   Available measurement units are: 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB',
   'MiB', 'GiB', 'TiB', 'PiB', 'EiB', 'ZiB', 'YiB'.

   Relative values use the size of the containing device or physical volume
   as a base. For example, specifying "40%" for a 100MiB device would result
   in a 40MiB partition. Relative sizes cannot be used for disks.

   You can also specify "remaining" as a size value for a volume in a disk or
   volume group. When "remaining" is specified, all remaining free space on
   the drive after allocations are made for all other volumes will be used for
   this volume.

.. _partitions_partition:

partition
"""""""""

- type - "partition". Required.
- size - Size of partition. Required.
- mount - Mount point, e.g. "/", "/usr". Optional (not mounted by default).
- file_system - File system type. Passed down to mkfs call.
  Optional (xfs by default).
- disk_label - Filesystem label. Optional (empty by default).
- partition_guid - GUID that will be assigned to partition. Optional.
- fstab_enabled - boolean value that specifies whether the partition will be
  included in /etc/fstab and mounted. Optional (true by default).
- fstab_options - string to specify fstab mount options.
  Optional ('defaults' by default).
- keep_data - Boolean flag specifying whether or not to preserve data on this
  partition. Applied when *verify* partitions_policy is used. Optional (True
  by default).
- images - A list of strings, specifies the images this partition belongs to.
  Belonging to an image means that the partition will be mounted into the filesystem
  tree before the image deployment, and then included into fstab file of the filesystem
  tree. Applies to multiboot node deployments (More than 1 image). Images are referred
  by name specified in "name" attribute of the image (see :ref:`images`).
  Optional (by default the partition belongs to the first image in the list of
  images). Example: *"images": ["centos", "ubuntu"]*.

.. warning:: If you are using the bareon swift deployment driver (bareon_swift_*),
   care must be taken when declaring mount points in your deployment
   configuration file that may conflict with those that exist in the tenant
   image. Doing this will cause the mount points defined in the deployment
   configuration to mask the corresponding directories in the tenant image
   when the deployment completes. For example, if your deployment
   configuration file contains a definition for '/etc/', the deployment will
   create an empty filesystem on disk and mount it on /etc in the tenant image.
   This will hide the contents of '/etc' from the original tenant image with
   the on-disk filesystem which was created during deployment.

.. _partitions_pv:

physical volume
"""""""""""""""

- type - "pv". Required.
- size - Size of the physical volume. Required.
- vg - id of the volume group this physical volume should belong to. Required.
- lvm_meta_size - a size that given to lvm to store metadata.
  Optional (64 MiB by default). Minimum allowable value: 10 MiB.

.. _partitions_vg:

volume group
""""""""""""

- type - "vg". Required.
- id - Volume group name. Should be refered at least once from pv. Required.
- volumes - Array of logical volumes. See below. Required.

.. _partitions_lv:

logical volume
""""""""""""""

- type - "lv". Required.
- name - Name of the logical volume. Required.
- size - Size of the logical volume. Required.
- mount - Mount point, e.g. "/", "/usr". Optional.
- file_system - File system type. Passed down to mkfs call.
  Optional (xfs by default).
- disk_label - Filesystem label. Optional (empty by default).
- images - A list of strings, specifies the images this volume belongs to.
  Belonging to an image means that the volume will be mounted into the filesystem
  tree before the image deployment, and then included into fstab file of the filesystem
  tree. Applies to multiboot node deployments (More than 1 image). Images are referred
  by name specified in "name" attribute of the image (see :ref:`images`).
  Optional (by default the partition belongs to the first image in the list of
  images). Example: *"images": ["centos", "ubuntu"]*.

.. warning:: If you are using the bareon swift deployment driver (bareon_swift_*),
   care must be taken when declaring mount points in your deployment
   configuration file that may conflict with those that exist in the tenant
   image. Doing this will cause the mount points defined in the deployment
   configuration to mask the corresponding directories in the tenant image
   when the deployment completes. For example, if your deployment
   configuration file contains a definition for '/etc/', the deployment will
   create an empty filesystem on disk and mount it on /etc in the tenant image.
   This will hide the contents of '/etc' from the original tenant image with
   the on-disk filesystem which was created during deployment.

.. note:: Putting a "/" partition on LVM requires a standalone "/boot" partition
   defined in the schema and the node should be managed by the bareon_rsync Ironic
   driver.

.. _images:

images
^^^^^^

An attribute called 'images' can be used to specify multiple images for the node
(a so called 'multiboot' node). It is an optional attribute, skip it if you don't
need more than 1 image deployed to the node. By default it will be a list of
one image: the one passed via --image arg of nova boot command.

An example of the deploy_config for two-image deployment below:

   ::

      {
          "images": [
              {
                  "name": "centos",
                  "url": "centos-7.1.1503",
                  "target": "/"
              },
              {
                  "name": "ubuntu",
                  "url": "ubuntu-14.04",
                  "target": "/"
              }
          ],
          "partitions": [
              {
                  "id": {
                      "type": "name",
                      "value": "vda"
                  },
                  "extra": [],
                  "free_space": "10000",
                  "volumes": [
                      {
                          "mount": "/",
                          "images": ["centos"],
                          "type": "partition",
                          "file_system": "ext4",
                          "size": "4000"
                      },
                      {
                          "mount": "/",
                          "images": ["ubuntu"],
                          "type": "partition",
                          "file_system": "ext4",
                          "size": "4000"
                      }
                  ],
                  "type": "disk",
                  "size": "10000"
              }
          ]
      }

During the multi-image deployment, the initial boot image is specified via
nova --image attribute. For example with the config shown above, if you need the
node to start from ubuntu, pass '--image ubuntu-14.04' to nova boot.

The process of switching of the active image described in :ref:`switch_boot_image`
section.

Images JSON attributes and their effect described below.

.. _images_name:

**name**

An alias name of the image. Used to be referred from the 'images' attribute of the
partition or logical volume (see :ref:`partitions_partition`, :ref:`partitions_lv`).
Required.

.. _images_url:

**url**

Name or UUID of the image in Glance. Required.

.. _images_target:

**target**

A point in the filesystem tree where the image should be deployed to. Required.
For all the standard cloud images this will be a *"/"*. Utility images can have
a different value, like */usr/share/utils*. Example below:

   ::

      {
          "images": [
              { # Centos image },
              { # Ubuntu image },
              {
                  "name": "utils",
                  "url": "utils-ver1.0",
                  "target": "/usr/share/utils"
              }
          ],
          "partitions": [
              {
                  ...
                  "volumes": [
                      {
                          "mount": "/",
                          "images": ["centos", "utils"],
                          "type": "partition",
                          "file_system": "ext4",
                          "size": "4000"
                      },
                      {
                          "mount": "/",
                          "images": ["ubuntu", "utils"],
                          "type": "partition",
                          "file_system": "ext4",
                          "size": "4000"
                      }
                  ]
              }
          ]
      }

In this case both Centos and Ubuntu images will get */usr/share/utils* directory
populated from the "utils-ver1.0" image.

Alternatively utilities image can be deployed to a standalone partition. Example
below:

   ::

      {
          "images": [
              { # Centos image },
              { # Ubuntu image },
              {
                  "name": "utils",
                  "url": "utils-ver1.0",
                  "target": "/usr/share/utils"
              }
          ],
          "partitions": [
              {
                  "volumes": [
                      {
                          # Centos root
                          "images": ["centos"],
                      },
                      {
                          # Ubuntu root
                          "images": ["ubuntu"],
                      },
                      {
                          "mount": "/usr/share/utils",
                          "images": ["centos", "ubuntu", "utils"],
                          "type": "partition",
                          "file_system": "ext4",
                          "size": "2000"
                      }
                  ],
                  ...
              }
          ]
      }

In this case the utilities image is deployed to it's own partition, which is
included into fstab file of both Centos and Ubuntu. Note that partition "images"
list also includes "utils" image as well. This is required for correct deployment:
the utils partition virtually belongs to "utils" image, and mounted
to the fs tree before "utils" image deployment (fake root in this case).

image_deploy_flags
^^^^^^^^^^^^^^^^^^

The attribute image_deploy_flags is composed in JSON, and is used to set flags
in the deployment tool. Optional (default for the "rsync_flags"
attribute is "-a -A -X"}).

.. note:: Currently used only by rsync.

The general structure is:

::

    "image_deploy_flags": {"rsync_flags": "-a -A -X --timeout 300"}


on_fail_script
^^^^^^^^^^^^^^

Carries a URL reference to a shell script (bash) executed inside ramdisk in case
of non-zero return code from bareon. Optional (default is empty shell).

General structure is:

::

    "on_fail_script": "my_on_fail_script.sh"


Where my_on_fail_script.sh is the URL pointing to the object in resource storage.
To add your script to default resource storage (Glance), use the following commands:

.. code-block:: console

   root@example # cat my_on_fail_script.sh
   cat /proc/cmdline
   ps aux | grep -i ssh
   dmesg | tail

   root@example # glance image-create --visibility public --disk-format raw \
   --container-format bare --name my_on_fail_script.sh \
   --file my_on_fail_script.sh

See :ref:`url_resolution` for information on how to use alternate storage sources.

Once the script is executed, the output is printed to Ironic-Conductor log.

.. _implicitly_taken_space:

Implicitly taken space in partitions schema
-------------------------------------------

In the example of partitions schema you may have noticed uneven size values like
4976. This is because bareon driver implicitly creates a number of partitions/spaces:

- For every disk in schema bareon driver creates a 24 MiB partition at the beginning.
  This is to allow correct installation of Grub Stage 1.5 data. It is implicitly
  created for every disk in schema even if the disk does not have /boot partition.
  Thus if 10000 MiB disk size is declared by schema, 9876 MiB is available
  for partitions/pvs. 24 MiB value is not configurable.

- Every physical volume has a 64 MiB less space than in takes on disk. If you
  declare a physical volume of size 5000 MiB, the volume group will get 4936 MiB
  available. If there are two physical volumes of 5000 MiB, the resulting
  volume group will have 9872 MiB (10000 - 2*64) available. This extra space is
  left for LVM metadata. 64 MiB value can be overriden by lvm_meta_size attribute
  of the pv, see :ref:`partitions_pv`.

- In case of multi-image deployment (see :ref:`images`) an additional 100 MiB partition
  is created on the boot disk (the 1st disk referred from deploy_config). This
  partition is used to install grub.

The partitions schema example is written to take all the declared space. Usually
you don't need to precisely calculate how much is left. You may leave for example
100 MiB free on each disk, and about 100-200 MiB in each volume group, depending
of how many physical volumes are in the group. Alternatively you can use a
"remaining" keyword to let bareon driver calculate for you, see :ref:`partitions_disk`.

.. _url_resolution:

URL resolution in Bareon Ironic driver
--------------------------------------

References given to the bareon driver (e.g. deploy_config) as well as references
from the deploy_config (e.g. on_fail_script) are URLs. Currently 4 types of
URL sources are supported:

- **glance**. URL structure is "glance:<image name | uuid>". Or simply
  "<image name | uuid>" if default_resource_storage_prefix is set to "glance:"
  (default value) in */etc/ironic/ironic.conf* . This storage uses user-context
  based authorization and thus has tenant isolation.

- **swift**. URL structure is "swift:container_name/object_name".
  Simply "object_name" or "container_name/object_name" can be used if
  default_resource_storage_prefix set appropriately in
  */etc/ironic/ironic.conf*. This storage uses user-context based authorization
  and thus has tenant isolation.

.. note:: Due to Ironic API limitation, to use a swift resource during
   deployment (e.g. '--meta deploy_config=swift:*'), the user should have an
   'admin' role in his tenant.

- **http**. URL structure is "http://site/path_to_resource". Contents should
  be directly accessible via URL, with no auth. Use "raw" links in services like
  http://paste.openstack.org/. The default_resource_storage_prefix option of
  */etc/ironic/ironic.conf* can be used to shorten the URL, e.g. set
  to "http://my-config-site/raw/". This storage does not support
  authentication/authorization and thus it does not have tenant isolation.


- **rsync**. URL structure is "rsync:SERVER_IP::rsync_module/path_to_resource".
  To use this kind of URL, the rsync server IP should be accessible from
  Ironic Conductor nodes. The default_resource_storage_prefix option of
  */etc/ironic/ironic.conf* can be used to shorten the URL, e.g. set to
  "rsync:SERVER_IP::rsync_module/". This storage does not support
  authentication/authorization and thus it does not have tenant isolation.


The "default_resource_storage_prefix" option can be used to
shorten the URL for the most frequently used URL type. If set, it can still
be overridden if the full URL is passed. For example, if the option is set to
"http://my-config-site/raw/", you can still use another http site if you specify
a full URL like: "http://my-other-config-site/raw/resource_id". If another storage
type is needed, use the full URL to specify that source.

Bareon Ironic driver actions
----------------------------

The ironic bareon driver can execute arbitrary user actions provided in a JSON
file describing the actions to be performed. This file has a number of
sections to control the execution of these actions. These sections and their
effect are described below.

Creating A Bareon Actions List
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To use Ironic bareon driver actions execution you must create an action list in
the resource storage to reference.

Create a JSON configuration file that defines the desired action list to be
executed. For example:

.. code-block:: console

   root@example # cat actions_list_example
   {
   "action_key": "private-key-url",
   "action_user": "centos",
   "action_list":
      [
         {
            "cmd": "cat",
            "name": "print_config_file",
            "terminate_on_fail": true,
            "args": "/tmp/conf",
            "sudo": true,
            "resources":
               [
                  {
                   "name": "resource_1",
                   "url": "my-resource-url",
                   "mode": "push"
                   "target": "/tmp/conf"
                  }
               ]
         }
      ]
   }

The JSON structure explained in the next section.

.. warning:: Since an error in the JSON file will prevent the deploy from
   succeeding, you may want to validate the JSON syntax, for example by executing
   `python -m json.tool < actions_list_example`.

To use default resource storage (Glance), create a glance image using the JSON
deploy configuration file as input.

.. code-block:: console

   root@example # glance image-create --visibility public --disk-format raw \
   --container-format bare --name actions_list_example \
   --file actions_list_example

See :ref:`url_resolution` for information on how to use alternate storage sources.

Invoking Driver Actions
^^^^^^^^^^^^^^^^^^^^^^^

Actions can be invoked in two cases:

- during deployment, right after the bareon has run
  (reference to JSON file is passed via --meta driver_actions);
- at any time when node is deployed and running
  (invoked via vendor-passthru).

Execution during deployment
^^^^^^^^^^^^^^^^^^^^^^^^^^^

In order to execute actions during deployment, the Nova metadata must include
a reference to the desired action list JSON, in this
example ``driver_actions=actions_list_example``.  This may be specified as
part of the Nova boot command or as OS::Nova::Server metadata in a Heat
template. An example of the former:

.. code-block:: console

   root@example # nova boot --nic net-id=23c11dbb-421e-44ca-b303-41656a4e6344 \
   --image centos-7.1.1503.raw --flavor ironic_flavor \
   --meta deploy_config=deploy_config_example \
   --meta driver_actions=actions_list_example --key-name=default bareon_test

Execution on a working node
^^^^^^^^^^^^^^^^^^^^^^^^^^^

In order to execute actions whilst the node is running, you should specify
``exec_actions`` node-vendor-passthru method,
``driver_actions=actions_list_example`` property and node uuid.
For example:

.. code-block:: console

   root@example # ironic node-vendor-passthru --http-method POST \
   node_uuid exec_actions driver_actions=actions_list_example

.. _actions_json:

Actions List JSON Structure
---------------------------

.. _actions_json_key:

action_key
^^^^^^^^^^

An attribute called action_key holds a resource storage URL pointing to ssh
private key contents being used to establish ssh connection to the node.

Only sources with tenant isolation can be used for this URL. See
:ref:`url_resolution` for available storage sources.

::

   "action_key": "ssh_key_url"

.. note:: This parameter is ignored when actions are invoked during deployment.
   The Default bareon key is used.

.. _actions_json_user:

action_user
^^^^^^^^^^^

An attribute called action_user holds a name of the user used to establish
an ssh connection to the node with the key provided in action_key.

::

   "action_user":  "centos"

.. note:: This parameter is ignored when actions are invoked during deployment.
   Default bareon user is being used.

.. _actions_json_list:

action_list
^^^^^^^^^^^
An attribute called action_list holds a list of actions being applied
to the node. Actions are executed in the order in which they appear in the list.

General structure is:

::

   "action_list":
      [
         {
            "cmd": "cat",
            "name": "print_config_file",
            "terminate_on_fail": true,
            "args": "/tmp/conf",
            "sudo": true,
            "resources":
               [
                  {
                   "name": "resource_1",
                   "url": "my-resource-url-1",
                   "mode": "push",
                   "target": "/tmp/conf"
                  },
                  {
                   "name": "resource_2",
                   "url": "my-resource-url-2",
                   "mode": "push",
                   "target": "/tmp/other-file"
                  },
                  {
                   ...more resources
                  }
               ]
         },
         {
            ...more actions
         }
      ]


- cmd - shell command to execute. Required.
- args - arguments for cmd. Required.
- name - alpha-numeric name of the action. Required.
- terminate_on_fail - flag to specify if actions execution should be terminated
  in case of action failure. Required.
- sudo - flag to specify if execution should be executed with sudo. Required.
- resources - array of resources. See resource. Required. May be an empty list.

resource
""""""""

Defines the resource required to execute an action. General structure is:

::

   {
      "name": "resource_1",
      "url": "resource-url",
      "mode": "push",
      "target": "/tmp/conf"
   }

- name - alpha-numeric name of the resource. Required.
- url - a URL pointing to resource. See :ref:`url_resolution` for available
  storage sources.
- mode - resource mode. See below. Required.
- target - target file name on the node. Required.

Resource **mode** can take one of the following:

- **push**. A resource of this type is cached by the Ironic Conductor and
  uploaded to the node at target path.

- **pull**. A resource of this type is cached by the Ironic Conductor and the
  reference to the resource is passed to the node (the reference is written
  to the file specified by the 'target' attribute) so that it can be pulled
  as part of the action. The reference is an rsync path that allows the node
  to pull the resource from the conductor. A typical way to pull the
  resource is:

.. code-block:: console

   root@baremetal-node # rsync $(cat /ref/file/path) .


- **pull-mount**. Like resources in pull mode, the resource is cached and the
  reference is passed to the target node. However, pull-mount resources are
  assumed to be file system images and are mounted in loopback mode by the
  Ironic Conductor. This allows the referencing action to pull from the
  filesystem tree as is done during rsync-based deployments. The following
  example will pull the contents of the image to the /root/path:

.. code-block:: console

   root@baremetal-node # rsync -avz $(cat /ref/file/path) /root/path


- **pull-swift-tempurl**. For resources of this type, Ironic obtains a Swift
  tempurl reference to the object and writes this tempurl to the file
  specified by the resource 'target' attribute. The tempurl duration is
  controlled by the */etc/ironic/ironic.conf*:

  * for *glance:<ref>* URLs an option *swift_temp_url_duration* from [glance]
    section is used;
  * for *swift:<ref>* URLs an option *swift_native_temp_url_duration*
    from [swift] section is used.

.. note:: To use 'pull-swift-tempurl' resource with Glance store Glance must be
   set to have Swift as a backend.

.. note:: Although all the Glance images are stored in the same Swift container,
   tempurls obtained from Glance are considered tenant-isolated because the
   tenant is checked by Glance as part of the generation of the temporary URL.

Resources of all modes can be mixed in a single action.

.. _switch_boot_image:

Switching boot image in a 'Multiboot' node
------------------------------------------

If a node has more than one images deployed (see :ref:`images`), the user can
switch boot image in two ways. Both of them require Ironic Conductor to SSH to
the node, thus SSH user/key needs to be provided.

Switching via nova rebuild
^^^^^^^^^^^^^^^^^^^^^^^^^^

To list images available to boot:

.. code-block:: console

   root@example # nova show VM_NAME

In show result the 'metadata' attribute will show a list of available images, like:

.. code-block:: console

   "available_images": "[u'centos-7.1.1503', u'ubuntu-14.04']

Currently booted image is shown by 'image' attribute of the VM. Let's say the
current image is 'centos-7.1.1503'. To switch to 'ubuntu-14.04' do:

.. code-block:: console

   root@example # nova rebuild VM_NAME 'ubuntu-14.04' \
   --meta sb_key=swift:container/file --meta sb_user=centos

Alternatively you can use image UUID to refer the image.

Note sb_key and sb_user attributes passed to metadata. They stand for 'switch boot
user' and 'switch boot key'. They are the username and a URL pointing
to the SSH private key used to ssh to the node. This is similar to :ref:`actions_json`.

Nova VM will be in "rebuild_spawning" state during switch process. Once it is active
the Node will start booting the specified image. If switch did not happen,
issue another "nova show" and check for "switch_boot_error" attribute in VM metadata.

For single-boot nodes a rebuild command will trigger a standard rebuild flow:
redeploying the node from scratch.

For multiboot nodes it is still possible to trigger a standard rebuild flow using
force_rebuild meta flag:

.. code-block:: console

   root@example # nova rebuild VM_NAME 'ubuntu-14.04' --meta force_rebuild=True

Switching via ironic node vendor-passthru
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To list images available to boot:

.. code-block:: console

   root@example # ironic node-show NODE_NAME

In show result the 'instance_info/multiboot_info/elements' attribute will carry a
list of available images. Every element has 'grub_id' which shows the ID in grub menu.
An 'instance_info/multiboot_info/current_element' shows the ID of the currently
selected image. To switch to another image do:

.. code-block:: console

   root@example # ironic node-vendor-passthru NODE_NAME switch_boot \
   image=<Name|UUID> ssh_key=swift:container/file ssh_user=centos

The API is synchronous, it will block until the switch is done. Node will start
booting the new image once it is done. If nothing happened, issue another
'ironic node-show' and check the last_error attribute.

.. note:: If ironic CLI is used to switch boot device, nova VM 'image', as well as Ironic
   'instance_info/image_source' are not updated to point the currently booted image.


Rebuilding nodes (nova rebuild)
-------------------------------

Since bareon driver requires deploy_config reference passed to work, during rebuild
process, the user has two options:

- Add --meta deploy_config=new_deploy_config attribute to the 'nova rebuild' command.
  The new deploy_config will be used to re-deploy the node.
- Skip --meta attribute. In this case deploy_config reference used during original
  deployment will be used.

Same applies to driver_actions.


Deployment termination
----------------------

Deployment can be terminated in both silent (wait-callback) and active
(deploying) phases using plain nova delete command.


.. code-block:: console

   root@example # node delete <VM_NAME>
