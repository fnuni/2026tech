"""Untuned admission-cap sensitivity at the design-day and concentrated load."""
import json
from pathlib import Path
from cell_model import Cell,Instance,simulate
from run_study import diagnostics,dump
from run_campaign import write_csv
ROOT=Path(__file__).resolve().parents[1]
def main():
    rows=[]
    for name in ['cs6_fullday','cs8_day180']:
        cell=Cell(ROOT/'instances'/f'{name}_config.json');inst=Instance(cell,cell.cfg['samples'])
        for cap in [1,2,3,4]:
            result=simulate(inst,'BNA',trace=False,admission_cap=cap)
            _,_,metrics=diagnostics(inst,result,name,'BNA')
            rows.append({'case':name,'admission_cap':cap,**result['kpi'],
                         **{k:v for k,v in metrics.items() if k not in ('case','policy')}})
            dump(ROOT/'validation/admission'/name/(str(cap)+'.json'),result)
    write_csv(ROOT/'data/admission_sensitivity.csv',rows)
if __name__=='__main__':main()
