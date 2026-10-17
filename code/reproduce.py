"""Run the complete released study and all audits in dependency order."""
import os,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    env=dict(os.environ);env.setdefault('MPLCONFIGDIR',str(Path(tempfile.gettempdir())/'2026apsc-mpl'))
    subprocess.run([sys.executable,str(ROOT/'code/verify_package.py'),'--check-hashes'],cwd=ROOT,env=env,check=True)
    for script in ['test_model.py','run_study.py','exact_study.py','run_hard_wait.py',
                   'make_tables.py','make_figures.py','verify_package.py']:
        print('Running',script,flush=True)
        subprocess.run([sys.executable,str(ROOT/'code'/script)],cwd=ROOT,env=env,check=True)
if __name__=='__main__':main()
