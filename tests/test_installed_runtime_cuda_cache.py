"""Only reuse complete existing managed CUDA files in the isolated acceptance data."""
import os
from pathlib import Path
import unittest
import test_installed_runtime_model_cache as model_fixture
from tools import verify_installed_runtime as module


class InstalledRuntimeCudaCacheTests(unittest.TestCase):
    def folders(self):return model_fixture.InstalledRuntimeModelCacheTests.folders(self)
    def cuda(self,real):
        root=real/'runtime/cuda12'
        paths={
            'cublas64_12.dll':'nvidia/cublas/bin',
            'cublasLt64_12.dll':'nvidia/cublas/bin',
            'cudnn64_9.dll':'nvidia/cudnn/bin',
            'nvrtc64_120_0.dll':'nvidia/cuda_nvrtc/bin',
            'nvrtc-builtins64_129.dll':'nvidia/cuda_nvrtc/bin',
        }
        for name,folder in paths.items():
            path=root/folder/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic dll fixture; never loaded')
        return root
    def test_complete_existing_cuda_cache_is_shared_by_one_exact_nested_link(self):
        real,isolated,create,created=self.folders();cuda=self.cuda(real)
        records=module.link_shared_model_caches(real,isolated,create_link=create,reuse_cuda_runtime=True)
        self.assertTrue((isolated/'runtime/cuda12').samefile(cuda))
        self.assertEqual([r['cache'] for r in records],['models','translation_models','runtime/cuda12'])
        self.assertTrue(all(r['shared'] for r in records));self.assertEqual(len(created),3)
        module.link_shared_model_caches(real,isolated,create_link=create,reuse_cuda_runtime=True)
        self.assertEqual(len(created),3)
        self.assertFalse((isolated/'runtime').samefile(real/'runtime'))
    def test_absent_formal_cuda_keeps_original_automatic_fallback_or_preparation_policy(self):
        real,isolated,create,created=self.folders()
        records=module.link_shared_model_caches(real,isolated,create_link=create,reuse_cuda_runtime=True)
        self.assertEqual(len(created),2);self.assertFalse((real/'runtime').exists())
        self.assertFalse((isolated/'runtime').exists());self.assertFalse(records[-1]['shared'])
        self.assertEqual(records[-1]['cache'],'runtime/cuda12')
    def test_existing_incomplete_cuda_is_rejected_before_any_links_or_repair(self):
        real,isolated,create,created=self.folders();cuda=self.cuda(real)
        missing=cuda/'nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll';missing.unlink()
        with self.assertRaises(ValueError):module.link_shared_model_caches(real,isolated,create_link=create,reuse_cuda_runtime=True)
        self.assertFalse(created);self.assertFalse(missing.exists())
    def test_cpu_and_onnx_modes_do_not_require_or_link_cuda(self):
        for model,device,expected in [('large-v3-turbo','NVIDIA GPU（float16）',True),
             ('large-v3-turbo','自动（优先GPU）',True),('large-v3-turbo','CPU（int8）',False),
             ('sensevoice-int8','自动（优先GPU）',False),('zipformer-zh-en-int8','NVIDIA GPU（float16）',False)]:
            with self.subTest(model=model,device=device):
                self.assertIs(module.recognition_may_use_cuda(dict(recognition_model=model,recognition_device=device)),expected)
        real,isolated,create,created=self.folders();(real/'runtime/cuda12').mkdir(parents=True)
        module.link_shared_model_caches(real,isolated,create_link=create,reuse_cuda_runtime=False)
        self.assertEqual(len(created),2);self.assertFalse((isolated/'runtime').exists())
    def test_different_target_or_runtime_parent_alias_is_preserved_and_rejected(self):
        real,isolated,create,created=self.folders();cuda=self.cuda(real)
        destination=isolated/'runtime/cuda12';destination.mkdir(parents=True)
        marker=destination/'keep.txt';marker.write_text('keep')
        with self.assertRaises(ValueError):module.link_shared_model_caches(real,isolated,create_link=create,reuse_cuda_runtime=True)
        self.assertEqual(marker.read_text(),'keep');self.assertFalse(created)
        marker.unlink();destination.rmdir();destination.parent.rmdir()
        create(isolated/'runtime',real/'runtime')
        with self.assertRaises(ValueError):module.link_shared_model_caches(real,isolated,create_link=create,reuse_cuda_runtime=True)
        self.assertEqual(len(created),1)
    def test_uncreated_cuda_link_is_not_reported_as_shared(self):
        real,isolated,create,created=self.folders();self.cuda(real)
        module.link_shared_model_caches(real,isolated,create_link=create)
        with self.assertRaises(RuntimeError):module.link_shared_model_caches(real,isolated,create_link=lambda *_:None,reuse_cuda_runtime=True)
        self.assertEqual(len(created),2)
    def test_nonbool_reuse_and_invalid_saved_selection_are_rejected(self):
        real,isolated,create,created=self.folders()
        for value in [1,0,'true',None]:
            with self.subTest(value=value),self.assertRaises(TypeError):
                module.link_shared_model_caches(real,isolated,create_link=create,reuse_cuda_runtime=value)
        self.assertFalse(created)
        with self.assertRaises(TypeError):module.recognition_may_use_cuda(dict(recognition_model=None,recognition_device='auto'))
    def test_required_dll_names_follow_the_production_gpu_runtime_contract(self):
        from gpu_runtime import _REQUIRED_DLLS
        self.assertEqual(module.CUDA_RUNTIME_DLLS,_REQUIRED_DLLS)


if __name__=='__main__':unittest.main()
