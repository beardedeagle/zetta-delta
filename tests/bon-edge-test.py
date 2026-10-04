#!/usr/bin/env python3
"""Bounded Git handoff regressions; all repositories/configuration are temporary."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
BON = Path(os.environ.get('BON', REPO / 'skills/orchestrate/scripts/bon.sh'))
BASELINE = Path(os.environ.get('BON_BASELINE_TEST', REPO / 'tests/bon-test.sh'))

class BonEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bon-edges-')
        self.t = Path(self.tmp.name).resolve()
        for p in ('home','hooks','tmp'): (self.t/p).mkdir()
        self.env = dict(os.environ, HOME=str(self.t/'home'), XDG_CACHE_HOME=str(self.t/'cache'),
                        XDG_CONFIG_HOME=str(self.t/'home/config'), TMPDIR=str(self.t/'tmp'),
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='core.hooksPath',
                        GIT_CONFIG_VALUE_0=str(self.t/'hooks'),GIT_CONFIG_KEY_1='core.fsmonitor',
                        GIT_CONFIG_VALUE_1='false',PYTHONDONTWRITEBYTECODE='1')
        for k in ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_OBJECT_DIRECTORY',
                  'GIT_ALTERNATE_OBJECT_DIRECTORIES'): self.env.pop(k,None)

    def tearDown(self): self.tmp.cleanup()

    def cmd(self, cwd, *args, check=True, env=None):
        p=subprocess.run(args,cwd=cwd,env=env or self.env,text=True,
                         stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=20)
        if check: self.assertEqual(p.returncode,0,p.stdout)
        return p

    def git(self,cwd,*args,check=True): return self.cmd(cwd,'git',*args,check=check)

    def repo(self,name,files):
        p=self.t/name;p.mkdir();self.git(p,'-c','init.defaultBranch=main','init','-q')
        self.git(p,'config','user.name','Fixture');self.git(p,'config','user.email','fixture@example.invalid')
        for f,body in files.items():
            path=p/f;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
        self.git(p,'add','-A');self.git(p,'commit','-qm','fixture');return p

    def bon(self,cwd,*args,check=True,env=None):
        return self.cmd(cwd,'sh',str(BON),*args,check=check,env=env)

    def room(self,files=None,untracked=None,staged=None,working=None,no_local=False):
        p=self.repo('parent',files or {'a.txt':'one\ntwo\nthree\n'})
        for f,b in (untracked or {}).items():(p/f).write_text(b)
        for f,b in (staged or {}).items():(p/f).write_text(b)
        if staged:self.git(p,'add','--',*staged)
        for f,b in (working or {}).items():(p/f).write_text(b)
        out=self.bon(p,'snapshot').stdout
        fields=dict(l.split('=',1) for l in out.splitlines() if l.startswith(('RUN=','TREE=')))
        c=self.t/'copy';self.git(self.t,'clone','-q','--no-local' if no_local else '--no-hardlinks',str(p),str(c))
        for f,b in (untracked or {}).items():(c/f).write_text(b)
        for f,b in (staged or {}).items():(c/f).write_text(b)
        if staged:self.git(c,'add','--',*staged)
        for f,b in (working or {}).items():(c/f).write_text(b)
        self.bon(c,'begin',fields['RUN'],'winner',fields['TREE'])
        self.r,self.tree,self.p,self.c=fields['RUN'],fields['TREE'],p,c
        self.patch=self.t/'cache/delta-orch'/self.r/'winner.patch'
        return p,c

    def export(self,check=True):return self.bon(self.c,'export',self.r,'winner',self.tree,check=check)

    def test_suite_failure_has_nonzero_exit(self):
        wrapper=self.t/'fail-bon.sh'
        wrapper.write_text('#!/bin/sh\nsh '+shlex_quote(str(BON))+' "$@"\n'
                           'if [ "$1" = clean ]; then echo "RESULT: failed: injected assertion"; fi\n')
        result=self.cmd(self.t,'sh',str(BASELINE),check=False,env=dict(self.env,BON=str(wrapper)))
        self.assertIn('SOME FAILED',result.stdout)
        self.assertNotEqual(result.returncode,0,result.stdout)

    def test_parent_deletion_survives_modify_delete_conflict(self):
        p,c=self.room({'a.txt':'base\n','keep.txt':'original\n'})
        (c/'a.txt').write_text('candidate\n'); self.export()
        (p/'keep.txt').write_text('staged\n');self.git(p,'add','keep.txt')
        (p/'keep.txt').write_text('unstaged\n');(p/'a.txt').unlink()
        result=self.bon(p,'apply',self.r,'winner',check=False)
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertFalse((p/'a.txt').exists())
        self.assertEqual(self.git(p,'show',':keep.txt').stdout,'staged\n')
        self.assertEqual((p/'keep.txt').read_text(),'unstaged\n')

    def test_completed_export_retry_preserves_patch_and_rejects_new_work(self):
        p,c=self.room();(c/'a.txt').write_text('candidate\n'); self.export()
        saved=self.patch.read_bytes();self.assertTrue(saved)
        again=self.export();self.assertEqual(self.patch.read_bytes(),saved)
        self.assertIn('RESTORED_TREE='+self.tree,again.stdout)
        (c/'a.txt').write_text('newer\n'); rejected=self.export(check=False)
        self.assertNotEqual(rejected.returncode,0,rejected.stdout)
        self.assertEqual((c/'a.txt').read_text(),'newer\n')
        self.assertEqual(self.patch.read_bytes(),saved)

    def test_assumption_flags_do_not_hide_snapshot_or_export_bytes(self):
        p=self.repo('parent',{'a.txt':'base\n'})
        self.git(p,'update-index','--assume-unchanged','a.txt');(p/'a.txt').write_text('parent\n')
        index=(p/'.git/index').read_bytes();out=self.bon(p,'snapshot').stdout
        tree=next(l[5:] for l in out.splitlines() if l.startswith('TREE='))
        self.assertEqual(self.git(p,'show',tree+':a.txt').stdout,'parent\n')
        self.assertEqual((p/'.git/index').read_bytes(),index)

    def test_candidate_assumption_flags_do_not_hide_export(self):
        p,c=self.room();self.git(c,'update-index','--assume-unchanged','a.txt')
        (c/'a.txt').write_text('candidate\n');self.export()
        self.assertTrue(self.patch.read_bytes())
        self.assertEqual((c/'a.txt').read_text(),'one\ntwo\nthree\n')

    def test_initial_untracked_file_becoming_ignored_is_restored(self):
        p,c=self.room({'a.txt':'base\n','.gitignore':''},{'u.txt':'original\n'})
        (c/'.gitignore').write_text('u.txt\n');out=self.export()
        self.assertEqual((c/'.gitignore').read_text(),'')
        self.assertEqual((c/'u.txt').read_text(),'original\n')
        self.assertIn('RESTORED_TREE='+self.tree,out.stdout)
        self.bon(p,'apply',self.r,'winner')
        self.assertEqual((p/'.gitignore').read_text(),'u.txt\n')
        self.assertEqual((p/'u.txt').read_text(),'original\n')

    def test_exact_special_paths_merge_without_staging_other_files(self):
        names=['café.txt','line\nbreak.txt',':(glob)*.txt']
        p,c=self.room({**{n:'one\ntwo\nthree\n' for n in names},'keep.txt':'original\n'})
        for name in names:(c/name).write_text('one\ntwo\nthree-candidate\n')
        self.export()
        for name in names:(p/name).write_text('one-parent\ntwo\nthree\n')
        (p/'keep.txt').write_text('staged\n');self.git(p,'add','keep.txt');(p/'keep.txt').write_text('unstaged\n')
        self.bon(p,'apply',self.r,'winner')
        for name in names:self.assertEqual((p/name).read_text(),'one-parent\ntwo\nthree-candidate\n')
        self.assertEqual(self.git(p,'show',':keep.txt').stdout,'staged\n')

    def test_cleanup_refuses_another_checkout(self):
        p,c=self.room();(c/'a.txt').write_text('candidate\n');self.export()
        result=self.bon(c,'clean',self.r,check=False)
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertTrue(self.patch.exists());self.git(p,'rev-parse','--verify','refs/orchestrate/'+self.r)

    def test_owner_cleanup_succeeds_when_ref_was_already_removed(self):
        p,c=self.room();(c/'a.txt').write_text('candidate\n');self.export()
        self.git(p,'update-ref','-d','refs/orchestrate/'+self.r)
        self.bon(p,'clean',self.r);self.assertFalse(self.patch.parent.exists())

    def test_check_detects_dirty_content_and_preserves_index_bytes(self):
        p,c=self.room();index=(p/'.git/index').read_bytes()
        self.bon(p,'check',self.r,self.tree)
        self.assertEqual((p/'.git/index').read_bytes(),index)
        (p/'a.txt').write_text('different\n');result=self.bon(p,'check',self.r,self.tree,check=False)
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertEqual((p/'.git/index').read_bytes(),index)
        self.assertEqual((p/'a.txt').read_text(),'different\n')

    def test_nonlocal_copy_materializes_dirty_snapshot_without_fetching_refs(self):
        p,c=self.room(working={'a.txt':'initial dirty\n'},untracked={'u.txt':'initial\n'},no_local=True)
        self.bon(p,'check',self.r,self.tree)
        index=(c/'.git/index').read_bytes();(c/'a.txt').write_text('candidate\n')
        self.export();self.assertEqual((c/'.git/index').read_bytes(),index)
        self.assertEqual((c/'a.txt').read_text(),'initial dirty\n')

    def test_unrelated_index_only_work_is_rejected_without_data_loss(self):
        p,c=self.room({'a.txt':'base\n','keep.txt':'original\n'})
        (c/'a.txt').write_text('candidate\n');self.git(c,'add','a.txt')
        (c/'keep.txt').write_text('later staged\n');self.git(c,'add','keep.txt')
        (c/'keep.txt').write_text('original\n');index=(c/'.git/index').read_bytes()
        result=self.export(check=False);self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertEqual((c/'.git/index').read_bytes(),index)
        self.assertEqual((c/'a.txt').read_text(),'candidate\n')
        self.assertEqual(self.git(c,'show',':keep.txt').stdout,'later staged\n')

    def test_completed_retry_preserves_later_index_only_work(self):
        p,c=self.room();(c/'a.txt').write_text('candidate\n');self.export()
        saved=self.patch.read_bytes();(c/'a.txt').write_text('later staged\n');self.git(c,'add','a.txt')
        (c/'a.txt').write_text('one\ntwo\nthree\n');index=(c/'.git/index').read_bytes()
        result=self.export(check=False);self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertEqual((c/'.git/index').read_bytes(),index)
        self.assertEqual(self.patch.read_bytes(),saved)

    def test_begin_is_idempotent_and_rejects_wrong_tree(self):
        p,c=self.room();self.bon(c,'begin',self.r,'winner',self.tree)
        result=self.bon(c,'begin',self.r,'winner','0'*40,check=False)
        self.assertNotEqual(result.returncode,0,result.stdout)

    def test_index_only_change_is_rejected_before_export_mutation(self):
        p,c=self.room({'a.txt':'base\n','.gitignore':''})
        self.git(c,'rm','--cached','a.txt');(c/'.gitignore').write_text('a.txt\n')
        index=(c/'.git/index').read_bytes();result=self.export(check=False)
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertIn('index',result.stdout.lower())
        self.assertEqual((c/'.git/index').read_bytes(),index)
        self.assertEqual((c/'a.txt').read_text(),'base\n')
        self.assertEqual((c/'.gitignore').read_text(),'a.txt\n')

    def test_staged_content_exports_and_restores_original_raw_index(self):
        p,c=self.room({'a.txt':'base\n','keep.txt':'original\n'},
                      staged={'keep.txt':'user-staged\n'},working={'keep.txt':'user-work\n'})
        original_index=(c/'.git/index').read_bytes()
        (c/'a.txt').write_text('candidate\n');(c/'new.txt').write_text('new\n')
        self.git(c,'add','a.txt','new.txt');self.export()
        self.assertEqual((c/'.git/index').read_bytes(),original_index)
        self.assertEqual((c/'a.txt').read_text(),'base\n')
        self.assertFalse((c/'new.txt').exists())
        self.assertEqual((c/'keep.txt').read_text(),'user-work\n')
        self.assertEqual(self.git(c,'show',':keep.txt').stdout,'user-staged\n')
        self.bon(p,'apply',self.r,'winner')
        self.assertEqual((p/'a.txt').read_text(),'candidate\n')
        self.assertEqual((p/'new.txt').read_text(),'new\n')
        self.assertEqual(self.git(p,'show',':keep.txt').stdout,'user-staged\n')

    def test_staged_file_deletion_exports_and_restores_original_raw_index(self):
        p,c=self.room({'a.txt':'base\n','keep.txt':'original\n'})
        original_index=(c/'.git/index').read_bytes();self.git(c,'rm','a.txt')
        self.export();self.assertEqual((c/'.git/index').read_bytes(),original_index)
        self.assertEqual((c/'a.txt').read_text(),'base\n')
        self.bon(p,'apply',self.r,'winner');self.assertFalse((p/'a.txt').exists())

    def test_export_retry_recovers_restored_content_with_index_lock_failure(self):
        p,c=self.room();original=(c/'.git/index').read_bytes()
        (c/'a.txt').write_text('candidate\n');self.git(c,'add','a.txt')
        lock=c/'.git/index.lock';lock.write_text('another Git operation\n')
        first=self.export(check=False);self.assertNotEqual(first.returncode,0,first.stdout)
        pending=self.patch.parent/'winner.pending/patch';saved=pending.read_bytes()
        self.assertTrue(saved);self.assertEqual((c/'a.txt').read_text(),'one\ntwo\nthree\n')
        self.assertEqual(lock.read_text(),'another Git operation\n')
        lock.unlink();self.export()
        self.assertEqual(self.patch.read_bytes(),saved)
        self.assertEqual((c/'.git/index').read_bytes(),original)

    def test_export_retry_recovers_atomic_publication_failure(self):
        p,c=self.room();(c/'a.txt').write_text('candidate\n')
        shim=self.t/'shim';shim.mkdir();realmv=shutil.which('mv');marker=self.t/'mv-failed'
        python=shutil.which('python3')
        (shim/'mv').write_text('#!'+python+'\nimport os,pathlib,sys\n'+
            'marker=pathlib.Path('+repr(str(marker))+')\n'+
            'if sys.argv[-1].endswith("winner.patch") and not marker.exists():\n'+
            ' marker.touch(); print("injected publication failure");sys.exit(1)\n'+
            'os.execv('+repr(realmv)+',['+repr(realmv)+',*sys.argv[1:]])\n')
        (shim/'mv').chmod(0o755);env=dict(self.env,PATH=str(shim)+':'+self.env['PATH'])
        first=self.bon(c,'export',self.r,'winner',self.tree,check=False,env=env)
        self.assertNotEqual(first.returncode,0,first.stdout)
        saved=(self.patch.parent/'winner.export/patch').read_bytes();self.assertTrue(saved)
        self.export();self.assertEqual(self.patch.read_bytes(),saved)

    def test_simultaneous_snapshots_have_distinct_owners(self):
        alpha=self.repo('alpha',{'a.txt':'alpha\n'});beta=self.repo('beta',{'a.txt':'beta\n'})
        shim=self.t/'shim';shim.mkdir();barrier=self.t/'barrier';barrier.mkdir()
        realgit=shutil.which('git');python=shutil.which('python3')
        (shim/'date').write_text('#!/bin/sh\nprintf "20261004-061111\\n"\n')
        (shim/'git').write_text('#!'+python+'\n'+
            'import os,pathlib,sys,time\n'+
            'if sys.argv[1:2]==["commit-tree"]:\n'+
            ' b=pathlib.Path(os.environ["BON_BARRIER"]);(b/pathlib.Path.cwd().name).touch()\n'+
            ' stop=time.monotonic()+5\n'+
            ' while len(list(b.iterdir()))<2 and time.monotonic()<stop:time.sleep(.01)\n'+
            'os.execv('+repr(realgit)+',['+repr(realgit)+',*sys.argv[1:]])\n')
        for name in ('date','git'):(shim/name).chmod(0o755)
        env=dict(self.env,PATH=str(shim)+':'+self.env['PATH'],BON_BARRIER=str(barrier))
        procs=[subprocess.Popen(['sh',str(BON),'snapshot'],cwd=p,env=env,text=True,
                  stdout=subprocess.PIPE,stderr=subprocess.STDOUT) for p in (alpha,beta)]
        runs=[]
        for proc in procs:
            out=proc.communicate(timeout=15)[0];self.assertEqual(proc.returncode,0,out)
            runs.append(next(l[4:] for l in out.splitlines() if l.startswith('RUN=')))
        self.assertNotEqual(*runs)
        for run,owner in zip(runs,(alpha,beta)):
            self.assertEqual((self.t/'cache/delta-orch'/run/'parent').read_text().strip(),str(owner))

    def test_snapshot_ref_collision_preserves_preexisting_ref(self):
        p=self.repo('parent',{'a.txt':'base\n'});run='bon-20261004-061111-Dup123'
        ref='refs/orchestrate/'+run;original=self.git(p,'rev-parse','HEAD').stdout.strip()
        self.git(p,'update-ref',ref,original)
        shim=self.t/'shim';shim.mkdir();real=shutil.which('mktemp');python=shutil.which('python3')
        (shim/'mktemp').write_text('#!'+python+'\nimport os,pathlib,sys\n'+
            'if "/delta-orch/bon-" in sys.argv[-1]:\n'+
            ' p=pathlib.Path(sys.argv[-1]).parent/'+repr(run)+';p.mkdir();print(p)\n'+
            'else: os.execv('+repr(real)+',['+repr(real)+',*sys.argv[1:]])\n')
        (shim/'mktemp').chmod(0o755)
        env=dict(self.env,PATH=str(shim)+':'+self.env['PATH'])
        result=self.bon(p,'snapshot',check=False,env=env)
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertEqual(self.git(p,'rev-parse',ref).stdout.strip(),original)
        self.assertFalse((self.t/'cache/delta-orch'/run).exists())

def shlex_quote(value):
    import shlex
    return shlex.quote(value)

if __name__=='__main__': unittest.main()
