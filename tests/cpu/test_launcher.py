import unittest
import intel_extension_for_pytorch as ipex
from common_utils import TestCase
import time, sys
from intel_extension_for_pytorch.cpu.launch import *
import os
from os.path import expanduser
import glob
import subprocess

class TestLauncher(TestCase):
    launch_scripts = [['python', '-m', 'intel_extension_for_pytorch.cpu.launch'],
                      ['ipexrun']]

    # examples
      # mode 0
      # 0 p0 0 | 4 p4 1
      # 1 p1 0 | 5 p5 1
      # 2 p2 0 | 6 p6 1
      # 3 p3 0 | 7 p7 1

      # 0 p0 0 | 4 l0 0 |  8 p4 1 | 12 l4 1
      # 1 p1 0 | 5 l1 0 |  9 p5 1 | 13 l5 1
      # 2 p2 0 | 6 l2 0 | 10 p6 1 | 14 l6 1
      # 3 p3 0 | 7 l3 0 | 11 p7 1 | 15 l7 1

      # mode 1
      # 0 p0 0 | 4 p4 1
      # 1 p1 0 | 5 p5 1
      # 2 p2 0 | 6 p6 1
      # 3 p3 0 | 7 p7 1

      # 0 p0 0 | 4 p4 1 |  8 l0 0 | 12 l4 1
      # 1 p1 0 | 5 p5 1 |  9 l1 0 | 13 l5 1
      # 2 p2 0 | 6 p6 1 | 10 l2 0 | 14 l6 1
      # 3 p3 0 | 7 p7 1 | 11 l3 0 | 15 l7 1

      # mode 2
      # 0 p0 0 | 4 p4 1
      # 1 p1 0 | 5 p5 1
      # 2 p2 0 | 6 p6 1
      # 3 p3 0 | 7 p7 1

      # 0 p0 0 | 4 p2 0 |  8 p4 1 | 12 p6 1
      # 1 l0 0 | 5 l2 0 |  9 l4 1 | 13 l6 1
      # 2 p1 0 | 6 p3 0 | 10 p5 1 | 14 p7 1
      # 3 l1 0 | 7 l3 0 | 11 l5 1 | 15 l7 1
    def construct_numa_config(self, num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=0, show_node=True):
        ret = ''
        ncores_per_node = n_phycores_per_node
        factor_ht = 1
        if enable_ht:
            factor_ht = 2
        ncores_per_node *= factor_ht
        num_cores = num_nodes * ncores_per_node
        for i in range(num_cores):
            cpu_idx = i
            core_idx = cpu_idx
            if numa_mode == 0:
                socket_idx = i // ncores_per_node
                core_idx = cpu_idx % n_phycores_per_node + socket_idx * n_phycores_per_node
            if numa_mode == 1:
                if enable_ht:
                    socket_idx = (i // n_phycores_per_node) % num_nodes
                    core_idx = cpu_idx % (n_phycores_per_node * num_nodes)
                else:
                    socket_idx = i // ncores_per_node
            if numa_mode == 2:
                socket_idx = i // ncores_per_node
                if enable_ht:
                    core_idx = (cpu_idx // factor_ht) % (n_phycores_per_node * num_nodes)
            node_idx = ''
            if show_node:
                node_idx = str(socket_idx)
            ret += f'{cpu_idx},{core_idx},{socket_idx},{node_idx}\n'
        return ret

    def find_lib(self, lib_type):
        library_paths = []
        if 'CONDA_PREFIX' in os.environ:
            library_paths.append(f'{os.environ["CONDA_PREFIX"]}/lib/')
        elif 'VIRTUAL_ENV' in os.environ:
            library_paths.append(f'{os.environ["VIRTUAL_ENV"]}/lib/')

        library_paths += [f'{expanduser("~")}/.local/lib/', '/usr/local/lib/',
                         '/usr/local/lib64/', '/usr/lib/', '/usr/lib64/']
        lib_find = False
        for lib_path in library_paths:
            library_file = f'{lib_path}/lib{lib_type}.so'
            matches = glob.glob(library_file)
            if len(matches) > 0:
                lib_find = True
                break
        return lib_find

    def del_env(self, env_name):
        if env_name in os.environ:
            del os.environ[env_name]

    def test_memory_allocator_setup(self):
        launcher = Launcher()

        # tcmalloc
        launcher.set_memory_allocator(memory_allocator='tcmalloc')
        find_tcmalloc = self.find_lib('tcmalloc')
        ld_preload_in_os = 'LD_PRELOAD' in os.environ
        tcmalloc_enabled = 'libtcmalloc.so' in os.environ['LD_PRELOAD'] if ld_preload_in_os else False
        self.assertEqual(find_tcmalloc, tcmalloc_enabled)

        # jemalloc
        launcher.set_memory_allocator(memory_allocator='jemalloc')
        find_jemalloc = self.find_lib('jemalloc')
        ld_preload_in_os = 'LD_PRELOAD' in os.environ
        jemalloc_enabled = 'libjemalloc.so' in os.environ['LD_PRELOAD'] if ld_preload_in_os else False
        self.assertEqual(find_jemalloc, jemalloc_enabled)
        if jemalloc_enabled:
            self.assertTrue('MALLOC_CONF' in os.environ)
            self.assertTrue(os.environ['MALLOC_CONF'] == 'oversize_threshold:1,background_thread:true,metadata_thp:auto')

        self.del_env('MALLOC_CONF')
        launcher.set_memory_allocator(memory_allocator='jemalloc', benchmark=True)
        if jemalloc_enabled:
            self.assertTrue('MALLOC_CONF' in os.environ)
            self.assertTrue(os.environ['MALLOC_CONF'] == 'oversize_threshold:1,background_thread:false,metadata_thp:always,dirty_decay_ms:-1,muzzy_decay_ms:-1')

    def test_mpi_pin_domain_and_ccl_worker_affinity(self):
        nprocs_per_node = 2
        ccl_worker_count = 4
        lscpu_txt = self.construct_numa_config(nprocs_per_node, 28, enable_ht=True, numa_mode=1)
        launcher = DistributedTrainingLauncher(lscpu_txt=lscpu_txt)

        launcher.cpuinfo.gen_pools_ondemand(ninstances=nprocs_per_node, use_logical_cores=True)
        pin_domain_affinity = launcher.get_pin_domain_affinity(launcher.cpuinfo.pools_ondemand, ccl_worker_count)
        expect_pin_domain = '[0xffffff0,0xffffff00000000]'
        self.assertEqual(pin_domain_affinity['pin_domain'], expect_pin_domain)
        expected_ccl_worker_affinity = '0,1,2,3,28,29,30,31'
        self.assertEqual(pin_domain_affinity['affinity'], expected_ccl_worker_affinity)

    def test_launcher_scripts(self):
        for launch_script in self.launch_scripts:
            cmd = launch_script + ['--help']
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(r.returncode, 0)

    def verify_affinity(self, pools, ground_truth):
        self.assertEqual(len(pools), ground_truth['ninstances'])
        self.assertEqual(len(pools[0]), ground_truth['ncores_per_instance'])
        self.assertEqual(len(set([c.cpu for p in pools for c in p])), ground_truth['num_cores_sum'])
        self.assertEqual(len(set([c.node for p in pools for c in p])), ground_truth['num_nodes_sum'])
        for i in range(ground_truth['ninstances']):
            self.assertEqual(len(set([c.cpu for c in pools[i]])), ground_truth['num_cores'][i])
            self.assertEqual(len(set([c.node for c in pools[i]])), ground_truth['num_nodes'][i])
            pool_txt = pools[i].get_pool_txt()
            self.assertEqual(pool_txt['cores'], ground_truth['pools_cores'][i])
            self.assertEqual(pool_txt['nodes'], ground_truth['pools_nodes'][i])

    def test_core_affinity(self):
        # mode 0
        num_nodes = 2
        n_phycores_per_node = 28
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=0)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        ground_truth = {
                'ninstances': 1,
                'ncores_per_instance': 112,
                'num_cores_sum': 112,
                'num_nodes_sum': 2,
                'num_cores': [112],
                'num_nodes': [2],
                'pools_cores': ['0-111'],
                'pools_nodes': ['0,1']}
        self.verify_affinity([cpuinfo.pool_all], ground_truth)

        cpuinfo.gen_pools_ondemand(ninstances=2)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 28,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [28, 28],
                'num_nodes': [1, 1],
                'pools_cores': ['0-27', '56-83'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ninstances=4)
        ground_truth = {
                'ninstances': 4,
                'ncores_per_instance': 14,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14, 14],
                'num_nodes': [1, 1, 1, 1],
                'pools_cores': ['0-13', '14-27', '56-69', '70-83'],
                'pools_nodes': ['0', '0', '1', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ncores_per_instance=28)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 28,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [28, 28],
                'num_nodes': [1, 1],
                'pools_cores': ['0-27', '56-83'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ncores_per_instance=14)
        ground_truth = {
                'ninstances': 4,
                'ncores_per_instance': 14,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14, 14],
                'num_nodes': [1, 1, 1, 1],
                'pools_cores': ['0-13', '14-27', '56-69', '70-83'],
                'pools_nodes': ['0', '0', '1', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cores_list_local = []
        cores_list_local.extend([i for i in range(14, 28)])
        cores_list_local.extend([i for i in range(42, 56)])
        cpuinfo.gen_pools_ondemand(cores_list=cores_list_local)
        ground_truth = {
                'ninstances': 1,
                'ncores_per_instance': 28,
                'num_cores_sum': 28,
                'num_nodes_sum': 1,
                'num_cores': [28],
                'num_nodes': [1],
                'pools_cores': ['14-27,42-55'],
                'pools_nodes': ['0']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        num_nodes = 4
        n_phycores_per_node = 14
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=0)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        ground_truth = {
                'ninstances': 1,
                'ncores_per_instance': 112,
                'num_cores_sum': 112,
                'num_nodes_sum': 4,
                'num_cores': [112],
                'num_nodes': [4],
                'pools_cores': ['0-111'],
                'pools_nodes': ['0,1,2,3']}
        self.verify_affinity([cpuinfo.pool_all], ground_truth)

        cpuinfo.gen_pools_ondemand(nodes_list=[1, 2])
        ground_truth = {
                'ninstances': 1,
                'ncores_per_instance': 28,
                'num_cores_sum': 28,
                'num_nodes_sum': 2,
                'num_cores': [28],
                'num_nodes': [2],
                'pools_cores': ['28-41,56-69'],
                'pools_nodes': ['1,2']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        # mode 1
        num_nodes = 2
        n_phycores_per_node = 28
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=1)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        cpuinfo.gen_pools_ondemand(ninstances=1)
        ground_truth = {
                'ninstances': 1,
                'ncores_per_instance': 56,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [56],
                'num_nodes': [2],
                'pools_cores': ['0-55'],
                'pools_nodes': ['0,1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ninstances=2)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 28,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [28, 28],
                'num_nodes': [1, 1],
                'pools_cores': ['0-27', '28-55'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ninstances=4)
        ground_truth = {
                'ninstances': 4,
                'ncores_per_instance': 14,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14, 14],
                'num_nodes': [1, 1, 1, 1],
                'pools_cores': ['0-13', '14-27', '28-41', '42-55'],
                'pools_nodes': ['0', '0', '1', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ncores_per_instance=28)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 28,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [28, 28],
                'num_nodes': [1, 1],
                'pools_cores': ['0-27', '28-55'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ncores_per_instance=14)
        ground_truth = {
                'ninstances': 4,
                'ncores_per_instance': 14,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14, 14],
                'num_nodes': [1, 1, 1, 1],
                'pools_cores': ['0-13', '14-27', '28-41', '42-55'],
                'pools_nodes': ['0', '0', '1', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cores_list_local = []
        cores_list_local.extend([i for i in range(14, 28)])
        cores_list_local.extend([i for i in range(42, 56)])
        cpuinfo.gen_pools_ondemand(ninstances=2, cores_list=cores_list_local)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 14,
                'num_cores_sum': 28,
                'num_nodes_sum': 2,
                'num_cores': [14, 14],
                'num_nodes': [1, 1],
                'pools_cores': ['14-27', '42-55'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        num_nodes = 4
        n_phycores_per_node = 14
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=1)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        cpuinfo.gen_pools_ondemand(ninstances=2, nodes_list=[1, 2])
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 14,
                'num_cores_sum': 28,
                'num_nodes_sum': 2,
                'num_cores': [14, 14],
                'num_nodes': [1, 1],
                'pools_cores': ['14-27', '28-41'],
                'pools_nodes': ['1', '2']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        # mode 2
        num_nodes = 2
        n_phycores_per_node = 28
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=2)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        cpuinfo.gen_pools_ondemand(ninstances=2)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 28,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [28, 28],
                'num_nodes': [1, 1],
                'pools_cores': ['0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46,48,50,52,54', '56,58,60,62,64,66,68,70,72,74,76,78,80,82,84,86,88,90,92,94,96,98,100,102,104,106,108,110'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ninstances=4)
        ground_truth = {
                'ninstances': 4,
                'ncores_per_instance': 14,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14, 14],
                'num_nodes': [1, 1, 1, 1],
                'pools_cores': ['0,2,4,6,8,10,12,14,16,18,20,22,24,26', '28,30,32,34,36,38,40,42,44,46,48,50,52,54', '56,58,60,62,64,66,68,70,72,74,76,78,80,82', '84,86,88,90,92,94,96,98,100,102,104,106,108,110'],
                'pools_nodes': ['0', '0', '1', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ncores_per_instance=28)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 28,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [28, 28],
                'num_nodes': [1, 1],
                'pools_cores': ['0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46,48,50,52,54', '56,58,60,62,64,66,68,70,72,74,76,78,80,82,84,86,88,90,92,94,96,98,100,102,104,106,108,110'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ncores_per_instance=14)
        ground_truth = {
                'ninstances': 4,
                'ncores_per_instance': 14,
                'num_cores_sum': 56,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14, 14],
                'num_nodes': [1, 1, 1, 1],
                'pools_cores': ['0,2,4,6,8,10,12,14,16,18,20,22,24,26', '28,30,32,34,36,38,40,42,44,46,48,50,52,54', '56,58,60,62,64,66,68,70,72,74,76,78,80,82', '84,86,88,90,92,94,96,98,100,102,104,106,108,110'],
                'pools_nodes': ['0', '0', '1', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cpuinfo.gen_pools_ondemand(ninstances=3)
        ground_truth = {
                'ninstances': 3,
                'ncores_per_instance': 18,
                'num_cores_sum': 54,
                'num_nodes_sum': 2,
                'num_cores': [18, 18, 18],
                'num_nodes': [1, 2, 1],
                'pools_cores': ['0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34', '36,38,40,42,44,46,48,50,52,54,56,58,60,62,64,66,68,70', '72,74,76,78,80,82,84,86,88,90,92,94,96,98,100,102,104,106'],
                'pools_nodes': ['0', '0,1', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        cores_list_local = []
        cores_list_local.extend([i for i in range(14, 28)])
        cores_list_local.extend([i for i in range(98, 112)])
        cpuinfo.gen_pools_ondemand(ninstances=2, cores_list=cores_list_local)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 14,
                'num_cores_sum': 28,
                'num_nodes_sum': 2,
                'num_cores': [14, 14],
                'num_nodes': [1, 1],
                'pools_cores': ['14-27', '98-111'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

        num_nodes = 4
        n_phycores_per_node = 14
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=2)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        cpuinfo.gen_pools_ondemand(nodes_list=[1, 2])
        ground_truth = {
                'ninstances': 1,
                'ncores_per_instance': 28,
                'num_cores_sum': 28,
                'num_nodes_sum': 2,
                'num_cores': [28],
                'num_nodes': [2],
                'pools_cores': ['28,30,32,34,36,38,40,42,44,46,48,50,52,54,56,58,60,62,64,66,68,70,72,74,76,78,80,82'],
                'pools_nodes': ['1,2']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

    def test_core_affinity_with_logical_cores(self):
        num_nodes = 2
        n_phycores_per_node = 28
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=1)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        cpuinfo.gen_pools_ondemand(ninstances=2, use_logical_cores=True)
        ground_truth = {
                'ninstances': 2,
                'ncores_per_instance': 56,
                'num_cores_sum': 112,
                'num_nodes_sum': 2,
                'num_cores': [56, 56],
                'num_nodes': [1, 1],
                'pools_cores': ['0-27,56-83', '28-55,84-111'],
                'pools_nodes': ['0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

    def test_core_affinity_with_skip_cross_node_cores(self):
        num_nodes = 2
        n_phycores_per_node = 28
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=1)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        cpuinfo.gen_pools_ondemand(ninstances=3, skip_cross_node_cores=True)
        ground_truth = {
                'ninstances': 3,
                'ncores_per_instance': 14,
                'num_cores_sum': 42,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14],
                'num_nodes': [1, 1, 1],
                'pools_cores': ['0-13', '14-27', '28-41'],
                'pools_nodes': ['0', '0', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

    def test_core_affinity_with_skip_cross_node_cores_and_use_logical_core(self):
        num_nodes = 2
        n_phycores_per_node = 28
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=1)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        cpuinfo.gen_pools_ondemand(ninstances=7, use_logical_cores=True, skip_cross_node_cores=True)
        ground_truth = {
                'ninstances': 7,
                'ncores_per_instance': 14,
                'num_cores_sum': 98,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14, 14, 14, 14, 14],
                'num_nodes': [1, 1, 1, 1, 1, 1, 1],
                'pools_cores': ['0-6,56-62', '7-13,63-69', '14-20,70-76', '21-27,77-83', '28-34,84-90', '35-41,91-97', '42-48,98-104'],
                'pools_nodes': ['0', '0', '0', '0', '1', '1', '1']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

    def test_core_affinity_with_skip_cross_node_cores_and_node_id_use_logical_core(self):
        num_nodes = 4
        n_phycores_per_node = 14
        lscpu_txt = self.construct_numa_config(num_nodes, n_phycores_per_node, enable_ht=True, numa_mode=1)
        cpuinfo = CPUPoolList(lscpu_txt=lscpu_txt)
        cpuinfo.gen_pools_ondemand(ninstances=3, nodes_list=[1, 2], use_logical_cores=True, skip_cross_node_cores=True)
        ground_truth = {
                'ninstances': 3,
                'ncores_per_instance': 14,
                'num_cores_sum': 42,
                'num_nodes_sum': 2,
                'num_cores': [14, 14, 14],
                'num_nodes': [1, 1, 1],
                'pools_cores': ['14-20,70-76', '21-27,77-83', '28-34,84-90'],
                'pools_nodes': ['1', '1', '2']}
        self.verify_affinity(cpuinfo.pools_ondemand, ground_truth)

if __name__ == '__main__':
    test = unittest.main()
