#!/usr/bin/python

# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""This module runs a suite of Auto Update tests.

  The tests can be run on either a virtual machine or actual device depending
  on parameters given.  Specific tests can be run by invoking --test_prefix.
  Verbose is useful for many of the tests if you want to see individual commands
  being run during the update process.
"""

import optparse
import os
import re
import sys
import thread
import time
import unittest
import urllib

sys.path.append(os.path.join(os.path.dirname(__file__), '../lib'))
from cros_build_lib import Die
from cros_build_lib import Info
from cros_build_lib import ReinterpretPathForChroot
from cros_build_lib import RunCommand
from cros_build_lib import RunCommandCaptureOutput
from cros_build_lib import Warning

import cros_test_proxy


class UpdateException(Exception):
  """Exception thrown when _UpdateImage or _UpdateUsingPayload fail"""
  def __init__(self, code, stdout):
    self.code = code
    self.stdout = stdout


class AUTest(object):
  """Abstract interface that defines an Auto Update test."""
  verbose = False

  def setUp(self):
    unittest.TestCase.setUp(self)
    # Set these up as they are used often.
    self.crosutils = os.path.join(os.path.dirname(__file__), '..')
    self.crosutilsbin = os.path.join(os.path.dirname(__file__))
    self.download_folder = os.path.join(self.crosutils, 'latest_download')
    if not os.path.exists(self.download_folder):
      os.makedirs(self.download_folder)

  # -------- Helper functions ---------

  def GetStatefulChangeFlag(self, stateful_change):
    """Returns the flag to pass to image_to_vm for the stateful change."""
    stateful_change_flag = ''
    if stateful_change:
      stateful_change_flag = '--stateful_update_flag=%s' % stateful_change

    return stateful_change_flag

  def _ParseGenerateTestReportOutput(self, output):
    """Returns the percentage of tests that passed based on output."""
    percent_passed = 0
    lines = output.split('\n')

    for line in lines:
      if line.startswith("Total PASS:"):
        # FORMAT: ^TOTAL PASS: num_passed/num_total (percent%)$
        percent_passed = line.split()[3].strip('()%')
        Info('Percent of tests passed %s' % percent_passed)
        break

    return int(percent_passed)

  def AssertEnoughTestsPassed(self, unittest, output, percent_required_to_pass):
    """Helper function that asserts a sufficient number of tests passed.

    Args:
      unittest: Handle to the unittest.
      output: stdout from a test run.
      percent_required_to_pass: percentage required to pass.  This should be
        fall between 0-100.
    Returns:
      percent that passed.
    """
    Info('Output from VerifyImage():')
    print >> sys.stderr, output
    sys.stderr.flush()
    percent_passed = self._ParseGenerateTestReportOutput(output)
    Info('Percent passed: %d vs. Percent required: %d' % (
        percent_passed, percent_required_to_pass))
    unittest.assertTrue(percent_passed >= percent_required_to_pass)
    return percent_passed

  def PerformUpdate(self, image_path, src_image_path='', stateful_change='old',
                    proxy_port=None):
    """Performs an update using  _UpdateImage and reports any error.

    Subclasses should not override this method but override _UpdateImage
    instead.

    Args:
      image_path:  Path to the image to update with.  This image must be a test
        image.
      src_image_path:  Optional.  If set, perform a delta update using the
        image specified by the path as the source image.
      stateful_change: How to modify the stateful partition.  Values are:
          'old':  Don't modify stateful partition.  Just update normally.
          'clean':  Uses clobber-state to wipe the stateful partition with the
            exception of code needed for ssh.
      proxy_port:  Port to have the client connect to. For use with
        CrosTestProxy.
    Raises an UpdateException if _UpdateImage returns an error.
    """
    try:
      if not self.use_delta_updates:
        src_image_path = ''

      self._UpdateImage(image_path, src_image_path, stateful_change, proxy_port)
    except UpdateException as err:
      # If the update fails, print it out
      Warning(err.stdout)
      raise

  def AttemptUpdateWithPayloadExpectedFailure(self, payload, expected_msg):
    """Attempt a payload update, expect it to fail with expected log"""
    try:
      self._UpdateUsingPayload(payload)
    except UpdateException as err:
      # Will raise ValueError if expected is not found.
      if re.search(re.escape(expected_msg), err.stdout, re.MULTILINE):
        return

    Warning("Didn't find '%s' in:" % expected_msg)
    Warning(err.stdout)
    self.fail('We managed to update when failure was expected')

  def AttemptUpdateWithFilter(self, filter, proxy_port=8081):
    """Update through a proxy, with a specified filter, and expect success."""

    self.PrepareBase(self.target_image_path)

    # The devserver runs at port 8080 by default. We assume that here, and
    # start our proxy at a different. We then tell our update tools to
    # have the client connect to our proxy_port instead of 8080.
    proxy = cros_test_proxy.CrosTestProxy(port_in=proxy_port,
                                          address_out='127.0.0.1',
                                          port_out=8080,
                                          filter=filter)
    proxy.serve_forever_in_thread()

    # This update is expected to fail...
    try:
      self.PerformUpdate(self.target_image_path, self.base_image_path,
                         proxy_port=proxy_port)
    finally:
      proxy.shutdown()

  # -------- Functions that subclasses should override ---------

  @classmethod
  def ProcessOptions(cls, parser, options):
    """Processes options.

    Static method that should be called from main.  Subclasses should also
    call their parent method if they override it.
    """
    cls.verbose = options.verbose
    cls.base_image_path = options.base_image
    cls.target_image_path = options.target_image
    cls.use_delta_updates = options.delta
    if options.quick_test:
      cls.verify_suite = 'build_RootFilesystemSize'
    else:
      cls.verify_suite = 'suite_Smoke'

    # Sanity checks.
    if not cls.base_image_path:
      parser.error('Need path to base image for vm.')
    elif not os.path.exists(cls.base_image_path):
      Die('%s does not exist' % cls.base_image_path)

    if not cls.target_image_path:
      parser.error('Need path to target image to update with.')
    elif not os.path.exists(cls.target_image_path):
      Die('%s does not exist' % cls.target_image_path)

  def PrepareBase(self, image_path):
    """Prepares target with base_image_path."""
    pass

  def _UpdateImage(self, image_path, src_image_path='', stateful_change='old',
                   proxy_port=None):
    """Implementation of an actual update.

    See PerformUpdate for description of args.  Subclasses must override this
    method with the correct update procedure for the class.
    """
    pass

  def _UpdateUsingPayload(self, update_path, stateful_change='old',
                         proxy_port=None):
    """Updates target with the pre-generated update stored in update_path.

    Subclasses must override this method with the correct update procedure for
    the class.

    Args:
      update_path:  Path to the image to update with. This directory should
        contain both update.gz, and stateful.image.gz
      proxy_port:  Port to have the client connect to. For use with
        CrosTestProxy.
    """
    pass

  def VerifyImage(self, percent_required_to_pass):
    """Verifies the image with tests.

    Verifies that the test images passes the percent required.  Subclasses must
    override this method with the correct update procedure for the class.

    Args:
      percent_required_to_pass:  percentage required to pass.  This should be
        fall between 0-100.

    Returns:
      Returns the percent that passed.
    """
    pass

  # -------- Tests ---------

  def testUpdateKeepStateful(self):
    """Tests if we can update normally.

    This test checks that we can update by updating the stateful partition
    rather than wiping it.
    """
    # Just make sure some tests pass on original image.  Some old images
    # don't pass many tests.
    self.PrepareBase(self.base_image_path)
    # TODO(sosa): move to 100% once we start testing using the autotest paired
    # with the dev channel.
    percent_passed = self.VerifyImage(10)

    # Update to - all tests should pass on new image.
    Info('Updating from base image on vm to target image.')
    self.PerformUpdate(self.target_image_path, self.base_image_path)
    self.VerifyImage(100)

    # Update from - same percentage should pass that originally passed.
    Info('Updating from updated image on vm back to base image.')
    self.PerformUpdate(self.base_image_path, self.target_image_path)
    self.VerifyImage(percent_passed)

  def testUpdateWipeStateful(self):
    """Tests if we can update after cleaning the stateful partition.

    This test checks that we can update successfully after wiping the
    stateful partition.
    """
    # Just make sure some tests pass on original image.  Some old images
    # don't pass many tests.
    self.PrepareBase(self.base_image_path)
    # TODO(sosa): move to 100% once we start testing using the autotest paired
    # with the dev channel.
    percent_passed = self.VerifyImage(10)

    # Update to - all tests should pass on new image.
    Info('Updating from base image on vm to target image and wiping stateful.')
    self.PerformUpdate(self.target_image_path, self.base_image_path, 'clean')
    self.VerifyImage(100)

    # Update from - same percentage should pass that originally passed.
    Info('Updating from updated image back to base image and wiping stateful.')
    self.PerformUpdate(self.base_image_path, self.target_image_path, 'clean')
    self.VerifyImage(percent_passed)

  # TODO(sosa): Get test to work with verbose.
  def NotestPartialUpdate(self):
    """Tests what happens if we attempt to update with a truncated payload."""
    # Preload with the version we are trying to test.
    self.PrepareBase(self.target_image_path)

    # Image can be updated at:
    # ~chrome-eng/chromeos/localmirror/autest-images
    url = 'http://gsdview.appspot.com/chromeos-localmirror/' \
          'autest-images/truncated_image.gz'
    payload = os.path.join(self.download_folder, 'truncated_image.gz')

    # Read from the URL and write to the local file
    urllib.urlretrieve(url, payload)

    expected_msg = 'download_hash_data == update_check_response_hash failed'
    self.AttemptUpdateWithPayloadExpectedFailure(payload, expected_msg)

  # TODO(sosa): Get test to work with verbose.
  def NotestCorruptedUpdate(self):
    """Tests what happens if we attempt to update with a corrupted payload."""
    # Preload with the version we are trying to test.
    self.PrepareBase(self.target_image_path)

    # Image can be updated at:
    # ~chrome-eng/chromeos/localmirror/autest-images
    url = 'http://gsdview.appspot.com/chromeos-localmirror/' \
          'autest-images/corrupted_image.gz'
    payload = os.path.join(self.download_folder, 'corrupted.gz')

    # Read from the URL and write to the local file
    urllib.urlretrieve(url, payload)

    # This update is expected to fail...
    expected_msg = 'zlib inflate() error:-3'
    self.AttemptUpdateWithPayloadExpectedFailure(payload, expected_msg)

  def testInterruptedUpdate(self):
    """Tests what happens if we interrupt payload delivery 3 times."""

    class InterruptionFilter(cros_test_proxy.Filter):
      """This filter causes the proxy to interrupt the download 3 times

         It does this by closing the first three connections to transfer
         2M total in the outbound connection after they transfer the
         2M.
      """
      def __init__(self):
        """Defines variable shared across all connections"""
        self.close_count = 0

      def setup(self):
        """Called once at the start of each connection."""
        self.data_size = 0

      def OutBound(self, data):
        """Called once per packet for outgoing data.

           The first three connections transferring more than 2M
           outbound will be closed.
        """
        if self.close_count < 3:
          if self.data_size > (2 * 1024 * 1024):
            self.close_count += 1
            return None

        self.data_size += len(data)
        return data

    self.AttemptUpdateWithFilter(InterruptionFilter(), proxy_port=8082)

  def testDelayedUpdate(self):
    """Tests what happens if some data is delayed during update delivery"""

    class DelayedFilter(cros_test_proxy.Filter):
      """Causes intermittent delays in data transmission.

         It does this by inserting 3 20 second delays when transmitting
         data after 2M has been sent.
      """
      def setup(self):
        """Called once at the start of each connection."""
        self.data_size = 0
        self.delay_count = 0

      def OutBound(self, data):
        """Called once per packet for outgoing data.

           The first three packets after we reach 2M transferred
           are delayed by 20 seconds.
        """
        if self.delay_count < 3:
          if self.data_size > (2 * 1024 * 1024):
            self.delay_count += 1
            time.sleep(20)

        self.data_size += len(data)
        return data

    self.AttemptUpdateWithFilter(DelayedFilter(), proxy_port=8083)

  def SimpleTest(self):
    """A simple update that updates once from a base image to a target.

    We explicitly don't use test prefix so that isn't run by default.  Can be
    run using test_prefix option.
    """
    self.PrepareBase(self.base_image_path)
    self.PerformUpdate(self.target_image_path, self.base_image_path)
    self.VerifyImage(100)


class RealAUTest(unittest.TestCase, AUTest):
  """Test harness for updating real images."""

  def setUp(self):
    AUTest.setUp(self)

  @classmethod
  def ProcessOptions(cls, parser, options):
    """Processes non-vm-specific options."""
    AUTest.ProcessOptions(parser, options)
    cls.remote = options.remote

    if not cls.remote:
      parser.error('We require a remote address for real tests.')

  def PrepareBase(self, image_path):
    """Auto-update to base image to prepare for test."""
    self.PerformUpdate(image_path)

  def _UpdateImage(self, image_path, src_image_path='', stateful_change='old',
                   proxy_port=None):
    """Updates a remote image using image_to_live.sh."""
    stateful_change_flag = self.GetStatefulChangeFlag(stateful_change)
    cmd = ['%s/image_to_live.sh' % self.crosutils,
           '--image=%s' % image_path,
           '--remote=%s' % self.remote,
           stateful_change_flag,
           '--verify',
           '--src_image=%s' % src_image_path
          ]

    if proxy_port:
      cmd.append('--proxy_port=%s' % proxy_port)

    if self.verbose:
      try:
        RunCommand(cmd)
      except Exception, e:
        raise UpdateException(1, e.message)
    else:
      (code, stdout, stderr) = RunCommandCaptureOutput(cmd)
      if code != 0:
        raise UpdateException(code, stdout)

  def _UpdateUsingPayload(self, update_path, stateful_change='old',
                         proxy_port=None):
    """Updates a remote image using image_to_live.sh."""
    stateful_change_flag = self.GetStatefulChangeFlag(stateful_change)
    cmd = ['%s/image_to_live.sh' % self.crosutils,
           '--payload=%s' % update_path,
           '--remote=%s' % self.remote,
           stateful_change_flag,
           '--verify',
          ]

    if proxy_port:
      cmd.append('--proxy_port=%s' % proxy_port)

    if self.verbose:
      try:
        RunCommand(cmd)
      except Exception, e:
        raise UpdateException(1, e.message)
    else:
      (code, stdout, stderr) = RunCommandCaptureOutput(cmd)
      if code != 0:
        raise UpdateException(code, stdout)

  def VerifyImage(self, percent_required_to_pass):
    """Verifies an image using run_remote_tests.sh with verification suite."""
    output = RunCommand([
        '%s/run_remote_tests.sh' % self.crosutils,
        '--remote=%s' % self.remote,
        self.verify_suite,
       ], error_ok=True, enter_chroot=False, redirect_stdout=True)
    return self.AssertEnoughTestsPassed(self, output, percent_required_to_pass)


class VirtualAUTest(unittest.TestCase, AUTest):
  """Test harness for updating virtual machines."""
  vm_image_path = None

  # VM Constants.
  _FULL_VDISK_SIZE = 6072
  _FULL_STATEFULFS_SIZE = 3074
  _KVM_PID_FILE = '/tmp/harness_pid'

  def _KillExistingVM(self, pid_file):
    if os.path.exists(pid_file):
      Warning('Existing %s found.  Deleting and killing process' %
              pid_file)
      RunCommand(['./cros_stop_vm', '--kvm_pid=%s' % pid_file],
                 cwd=self.crosutilsbin)

    assert not os.path.exists(pid_file)

  def setUp(self):
    """Unit test overriden method.  Is called before every test."""
    AUTest.setUp(self)
    self._KillExistingVM(self._KVM_PID_FILE)

  @classmethod
  def ProcessOptions(cls, parser, options):
    """Processes vm-specific options."""
    AUTest.ProcessOptions(parser, options)
    cls.board = options.board

    # Communicate flags to tests.
    cls.graphics_flag = ''
    if options.no_graphics: cls.graphics_flag = '--no_graphics'

    if not cls.board:
      parser.error('Need board to convert base image to vm.')

  def PrepareBase(self, image_path):
    """Creates an update-able VM based on base image."""
    self.vm_image_path = '%s/chromiumos_qemu_image.bin' % os.path.dirname(
        image_path)

    Info('Creating: %s' % self.vm_image_path)

    if not os.path.exists(self.vm_image_path):
      Info('Qemu image %s not found, creating one.' % self.vm_image_path)
      RunCommand(['%s/image_to_vm.sh' % self.crosutils,
                  '--full',
                  '--from=%s' % ReinterpretPathForChroot(
                      os.path.dirname(image_path)),
                  '--vdisk_size=%s' % self._FULL_VDISK_SIZE,
                  '--statefulfs_size=%s' % self._FULL_STATEFULFS_SIZE,
                  '--board=%s' % self.board,
                  '--test_image'], enter_chroot=True)
    else:
      Info('Using existing VM image %s' % self.vm_image_path)

    Info('Testing for %s' % self.vm_image_path)
    self.assertTrue(os.path.exists(self.vm_image_path))

  def _UpdateImage(self, image_path, src_image_path='', stateful_change='old',
                   proxy_port=None):
    """Updates VM image with image_path."""
    stateful_change_flag = self.GetStatefulChangeFlag(stateful_change)
    if src_image_path == self.base_image_path:
      src_image_path = self.vm_image_path

    cmd = ['%s/cros_run_vm_update' % self.crosutilsbin,
           '--update_image_path=%s' % image_path,
           '--vm_image_path=%s' % self.vm_image_path,
           '--snapshot',
           self.graphics_flag,
           '--persist',
           '--kvm_pid=%s' % self._KVM_PID_FILE,
           stateful_change_flag,
           '--src_image=%s' % src_image_path,
           ]

    if proxy_port:
      cmd.append('--proxy_port=%s' % proxy_port)

    if self.verbose:
      try:
        RunCommand(cmd)
      except Exception, e:
        raise UpdateException(1, e.message)
    else:
      (code, stdout, stderr) = RunCommandCaptureOutput(cmd)
      if code != 0:
        raise UpdateException(code, stdout)

  def _UpdateUsingPayload(self, update_path, stateful_change='old',
                         proxy_port=None):
    """Updates a vm image using cros_run_vm_update."""
    stateful_change_flag = self.GetStatefulChangeFlag(stateful_change)
    cmd = ['%s/cros_run_vm_update' % self.crosutilsbin,
           '--payload=%s' % update_path,
           '--vm_image_path=%s' % self.vm_image_path,
           '--snapshot',
           self.graphics_flag,
           '--persist',
           '--kvm_pid=%s' % self._KVM_PID_FILE,
           stateful_change_flag,
           ]

    if proxy_port:
      cmd.append('--proxy_port=%s' % proxy_port)

    if self.verbose:
      try:
        RunCommand(cmd)
      except Exception, e:
        raise UpdateException(1, e.message)
    else:
      (code, stdout, stderr) = RunCommandCaptureOutput(cmd)
      if code != 0:
        raise UpdateException(code, stdout)

  def VerifyImage(self, percent_required_to_pass):
    """Runs vm smoke suite to verify image."""
    # image_to_live already verifies lsb-release matching.  This is just
    # for additional steps.

    commandWithArgs = ['%s/cros_run_vm_test' % self.crosutilsbin,
                       '--image_path=%s' % self.vm_image_path,
                       '--snapshot',
                       '--persist',
                       '--kvm_pid=%s' % self._KVM_PID_FILE,
                       self.verify_suite,
                       ]

    if self.graphics_flag:
      commandWithArgs.append(self.graphics_flag)

    output = RunCommand(commandWithArgs, error_ok=True, enter_chroot=False,
                        redirect_stdout=True)
    return self.AssertEnoughTestsPassed(self, output, percent_required_to_pass)


def main():
  parser = optparse.OptionParser()
  parser.add_option('-b', '--base_image',
                    help='path to the base image.')
  parser.add_option('-r', '--board',
                    help='board for the images.')
  parser.add_option('--no_delta', action='store_false', default=True,
                    dest='delta',
                    help='Disable using delta updates.')
  parser.add_option('--no_graphics', action='store_true',
                    help='Disable graphics for the vm test.')
  parser.add_option('-m', '--remote',
                    help='Remote address for real test.')
  parser.add_option('-q', '--quick_test', default=False, action='store_true',
                    help='Use a basic test to verify image.')
  parser.add_option('-t', '--target_image',
                    help='path to the target image.')
  parser.add_option('--test_prefix', default='test',
                    help='Only runs tests with specific prefix i.e. '
                         'testFullUpdateWipeStateful.')
  parser.add_option('-p', '--type', default='vm',
                    help='type of test to run: [vm, real]. Default: vm.')
  parser.add_option('--verbose', default=True, action='store_true',
                    help='Print out rather than capture output as much as '
                         'possible.')
  (options, leftover_args) = parser.parse_args()

  if leftover_args:
    parser.error('Found extra options we do not support: %s' % leftover_args)

  if options.type == 'vm':  test_class = VirtualAUTest
  elif options.type == 'real': test_class = RealAUTest
  else: parser.error('Could not parse harness type %s.' % options.type)

  test_class.ProcessOptions(parser, options)

  test_loader = unittest.TestLoader()
  test_loader.testMethodPrefix = options.test_prefix
  test_suite = test_loader.loadTestsFromTestCase(test_class)
  test_result = unittest.TextTestRunner(verbosity=2).run(test_suite)

  if not test_result.wasSuccessful():
    Die('Test harness was not successful')


if __name__ == '__main__':
  main()
