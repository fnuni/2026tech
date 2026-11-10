"""CSV helper and compatibility entry point for the complete released study."""
import csv
from pathlib import Path

def write_csv(path,rows):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fields=[]
    for row in rows:
        for key in row:
            if key not in fields:fields.append(key)
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,restval='')
        writer.writeheader();writer.writerows(rows)

def main():
    from run_study import main as study
    study()
if __name__=='__main__':main()
