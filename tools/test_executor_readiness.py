"""Native adapter readiness contract; no private package bytes in public fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('executor_readiness',ROOT/'deploy/executor-readiness.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class ExecutorReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)/'factory-executor';self.namespace='example-engineering'
        self.package=self.home/'plugins'/self.namespace
        for name in module.SKILLS:
            directory=self.package/'skills'/name;directory.mkdir(parents=True)
            (directory/'SKILL.md').write_text('# Disposable test instruction')
        assets=self.package/'skills/prepare-pr/assets';assets.mkdir();(assets/'pr-description.md').write_text('Disposable template')
        (self.package/'plugin.yaml').write_text(json.dumps({'name':self.namespace,'version':'1.0.0'}))
        self.pin={'schema':'factory-executor-plugin-v1','namespace':self.namespace,'version':'1.0.0','source_commit':'a'*40,'archive_sha256':'b'*64,'tree_sha256':module.package_digest(self.package),'repository':'https://example.com/plugin'}
        (self.package/'.factory-source.json').write_text(json.dumps(self.pin))
        self.args=['--profile','factory-executor','--skills',','.join(self.namespace+':'+n for n in module.SKILLS)]
        (self.home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':[self.namespace]}}))
        self.calls=[]
        def view(name,preprocess=True,**kwargs):
            self.calls.append((name,preprocess))
            return json.dumps({'success':True,'skill_dir':str(self.package/'skills'/name.split(':')[1])})
        self.original_view=view
        self.modules={'pm.install':types.SimpleNamespace(venv_is_current=lambda:True),'ruamel.yaml':types.SimpleNamespace(YAML=lambda **kwargs:types.SimpleNamespace(load=json.loads)),
            'hermes_cli._parser':types.SimpleNamespace(PRE_ARGPARSE_INHERITED_FLAGS=[('--profile',True)],build_top_level_parser=lambda:(types.SimpleNamespace(parse_args=lambda args:types.SimpleNamespace(skills=[args[1]],oneshot='readiness-no-dispatch')),)),
            'hermes_cli.profiles':types.SimpleNamespace(resolve_profile_env=lambda name:str(self.home)),
            'hermes_constants':types.SimpleNamespace(get_hermes_home=lambda:self.home),
            'tools.skills_tool':types.SimpleNamespace(skill_view=view),
            'agent.skill_commands':types.SimpleNamespace(build_preloaded_skills_prompt=lambda names:('native prompt',[self.namespace+':'+name for name in module.SKILLS],[]))}

    def check(self,args=None):
        with patch.dict(sys.modules,self.modules),patch.object(module,'PROFILE_HOME',self.home),patch.dict(os.environ,{'HERMES_RUNTIME_DIR':'/opt/hermes/tools','FACTORY_HERMES_PROFILE':'factory-executor','FACTORY_HERMES_SKILL_NAMESPACE':self.namespace}):
            return module.check(self.args if args is None else args,reviewed=self.pin)

    def test_plugin_skills_template_and_cli_resolve_without_preprocessing(self):
        result=self.check();self.assertEqual(set(result['skills']),set(module.SKILLS));self.assertFalse(result['model_called']);self.assertTrue(result['cli_arguments_checked'])
        self.assertEqual(self.calls,[(self.namespace+':'+n,False) for n in module.SKILLS]+[(self.namespace+':prepare-pr',False)])
        self.assertFalse((self.home/'auth.json').exists())

    def test_actual_argv_and_missing_template_refuse(self):
        with self.assertRaisesRegex(ValueError,'arguments'):self.check(['--profile','factory-executor'])
        (self.package/'skills/prepare-pr/assets/pr-description.md').unlink()
        with self.assertRaisesRegex(ValueError,'immutable source'):self.check()

    def test_disabled_shadowed_and_partial_preload_refuse(self):
        self.modules['tools.skills_tool']=types.SimpleNamespace(skill_view=lambda *a,**k:json.dumps({'success':False}))
        with self.assertRaisesRegex(ValueError,'missing, disabled or shadowed'):self.check()
        self.modules['tools.skills_tool']=types.SimpleNamespace(skill_view=lambda *a,**k:json.dumps({'success':True,'skill_dir':'/other'}))
        with self.assertRaisesRegex(ValueError,'shadowed'):self.check()
        self.modules['tools.skills_tool']=types.SimpleNamespace(skill_view=self.original_view)
        self.modules['agent.skill_commands']=types.SimpleNamespace(build_preloaded_skills_prompt=lambda names:('partial',['implement-change'],['verify-app']))
        with self.assertRaisesRegex(ValueError,'all required skills'):self.check()

    def test_package_links_extra_files_and_source_receipt_mismatch_refuse(self):
        extra=self.package/'unreviewed.py';extra.write_text('unreviewed')
        with self.assertRaisesRegex(ValueError,'immutable source'):self.check()
        extra.unlink();(self.package/'.factory-source.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'immutable source'):self.check()
        (self.package/'.factory-source.json').write_text(json.dumps(self.pin))
        linked=self.package/'skills/prepare-pr/assets';shutil.rmtree(linked);linked.symlink_to(self.home,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symlink'):self.check()

    def test_cold_native_dependency_state_refuses_before_loading_plugins(self):
        self.modules['pm.install']=types.SimpleNamespace(venv_is_current=lambda:False)
        with self.assertRaisesRegex(ValueError,'dependency admission'):self.check()
        self.assertEqual(self.calls,[])

    def test_only_image_matching_existing_observer_is_permitted(self):
        observer=self.home/'plugins/observability/posthog'
        shutil.copytree(Path(module.__file__).parent/'factory-observers/posthog',observer)
        (self.home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':['observability/posthog',self.namespace]}}))
        self.check()
        (observer/'__init__.py').write_text('unreviewed adapter')
        with self.assertRaisesRegex(ValueError,'observer differs'):self.check()
        (self.home/'config.yaml').write_text(json.dumps({'plugins':{'enabled':[self.namespace,'other']}}))
        with self.assertRaisesRegex(ValueError,'unreviewed enabled'):self.check()


if __name__=='__main__':unittest.main()
