#!/usr/bin/env python3
"""Verify a Gateway wheel with fresh HOME, no runtime, fake coding and persisted history."""
import argparse,json,os,subprocess,sys,tempfile,time
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wheel',required=True,type=Path)
    p.add_argument('--receipt',required=True,type=Path)
    a=p.parse_args();started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='gateway-isolated-') as temp:
        root=Path(temp);(root/'home').mkdir()
        env=dict(os.environ,HOME=str(root/'home'));env.pop('PYTHONPATH',None)
        venv=root/'venv'
        subprocess.run([sys.executable,'-m','venv',str(venv)],check=True,capture_output=True)
        python=str(venv/'bin/python')
        install=subprocess.run([python,'-m','pip','install',str(a.wheel.resolve())],capture_output=True,text=True,env=env)
        if install.returncode:raise RuntimeError(install.stderr[-1500:])
        fixture=root/'workflow.py'
        fixture.write_text((Path(__file__).parents[1]/'tests/fixtures/cloud_standalone.py').read_text())
        result=subprocess.run([python,'-I',str(fixture)],cwd=root,env=env,capture_output=True,text=True,timeout=60)
        if result.returncode:raise RuntimeError((result.stdout+result.stderr)[-3000:])
        receipt={'status':'passed','wheel':a.wheel.name,'fresh_home':True,'workflow':json.loads(result.stdout),
                 'duration_seconds':time.monotonic()-started}
        a.receipt.write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))

if __name__=='__main__':main()
