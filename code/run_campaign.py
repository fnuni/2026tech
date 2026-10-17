"""CSV helper and compatibility entry point for the complete released study."""
import csv
from pathlib import Path

def write_csv(path,rows):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)

def main():
    from run_study import main as study
    study()
if __name__=='__main__':main()
