from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import gzip,hashlib,json
from pathlib import Path
import tempfile,time
import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.csv as pac
import pyarrow.parquet as pq
from backtester.tick_data import TickCsvProfile,TickDatasetSpec,TickManifest,TICK_SCHEMA

import argparse
parser=argparse.ArgumentParser(description="Import the verified five-year QDM export in bounded batches")
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
source,output=args.source,args.output
output.parent.mkdir(parents=True,exist_ok=True)
expected_gzip='c35f7e5b70627d06655d01b8bdfdd2e7c1cadbfdb72e10660b36d1b0e4ac358d'
expected_csv='ceef28d3ad74c627eb33869bbdf6d32f0d52514f1e88ea0df57a477f445bdd40'
started=time.monotonic()
assert not output.exists()
with source.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==expected_gzip

def raw_checksum():
 digest=hashlib.sha256();size=0;tail=b''
 with gzip.open(source,'rb') as f:
  while block:=f.read(8*1024**2):digest.update(block);size+=len(block);tail=block[-1024:]
 actual=digest.hexdigest();assert actual==expected_csv,(actual,expected_csv)
 return {'csv_sha256':actual,'csv_bytes':size,'last_source_rows':tail.decode().splitlines()[-3:]}

profile=TickCsvProfile(fieldnames=['DateTime','Bid','Ask'],columns={'timestamp':'DateTime','bid':'Bid','ask':'Ask'},timestamp_format='%Y.%m.%d %H:%M:%S.%f',timezone='Europe/Kyiv')
spec=TickDatasetSpec(symbol='DEUIDXEUR',source='QuantDataManager five-year DEUIDXEUR bid/ask export')
with ThreadPoolExecutor(max_workers=1) as pool,tempfile.TemporaryDirectory(prefix='.qdm-5y-',dir=output.parent) as tmp:
 future=pool.submit(raw_checksum)
 parquet=Path(tmp)/'ticks.parquet'
 with duckdb.connect() as c:
  c.execute("SET threads=1");c.execute("SET memory_limit='512MB'");c.execute("SET TimeZone='UTC'")
  print(json.dumps({'stage':'copying_gzip_to_parquet','started_utc':datetime.now(timezone.utc).isoformat()}),flush=True)
  sql = """WITH raw AS (
    SELECT row_number() OVER ()-1+$offset AS sequence,
     strptime(DateTime,'%Y.%m.%d %H:%M:%S.%f') AS local_time,
     Bid AS bid, Ask AS ask FROM quote_chunk
   ), converted AS (SELECT *,local_time AT TIME ZONE 'Europe/Kyiv' AS utc_time FROM raw)
   SELECT CAST(sequence AS BIGINT) AS sequence,
    CASE WHEN utc_time AT TIME ZONE 'Europe/Kyiv' <> local_time THEN error('Nonexistent Kyiv source time')
     WHEN month(local_time) IN (3,10) AND dayofweek(local_time)=0 AND day(local_time)>=25 AND hour(local_time)=3
       AND (((utc_time+INTERVAL '1 hour') AT TIME ZONE 'Europe/Kyiv'=local_time)
         OR ((utc_time-INTERVAL '1 hour') AT TIME ZONE 'Europe/Kyiv'=local_time))
       THEN error('Ambiguous Kyiv source time: UTC source required')
     ELSE utc_time END AS timestamp,
    bid,ask,CAST(0 AS DOUBLE) AS volume FROM converted"""
  copied=0
  with pa.input_stream(str(source),compression='gzip') as incoming, pq.ParquetWriter(parquet,TICK_SCHEMA,compression='zstd') as writer:
   reader=pac.open_csv(incoming,read_options=pac.ReadOptions(column_names=['DateTime','Bid','Ask'],block_size=16*1024**2),convert_options=pac.ConvertOptions(column_types={'DateTime':pa.string(),'Bid':pa.float64(),'Ask':pa.float64()}))
   for batch in reader:
    c.register('quote_chunk',pa.Table.from_batches([batch]))
    converted=c.execute(sql,{'offset':copied}).fetch_arrow_table()
    assert converted.schema==TICK_SCHEMA
    writer.write_table(converted)
    c.unregister('quote_chunk')
    old=copied;copied+=len(batch)
    if copied//5000000 != old//5000000:
     print(json.dumps({'stage':'importing','rows':copied,'elapsed_seconds':time.monotonic()-started}),flush=True)
 print(json.dumps({'stage':'validating_all_quotes','parquet_bytes':parquet.stat().st_size,'elapsed_seconds':time.monotonic()-started}),flush=True)
 raw=future.result();print(json.dumps({'stage':'csv_checksum_verified',**raw}),flush=True)
 assert pq.read_schema(parquet)==TICK_SCHEMA,(pq.read_schema(parquet),TICK_SCHEMA)
 count=equal=maximum=0;first=previous=None
 for batch in pq.ParquetFile(parquet).iter_batches(batch_size=262144):
  ts=batch.column('timestamp').to_numpy().astype('datetime64[us]').astype(np.int64)
  seq=batch.column('sequence').to_numpy();bid=batch.column('bid').to_numpy();ask=batch.column('ask').to_numpy()
  assert np.array_equal(seq,np.arange(count,count+len(ts)))
  assert np.isfinite(bid).all() and np.isfinite(ask).all() and np.all(bid>0) and np.all(ask>=bid)
  d=np.diff(ts)
  if previous is not None:d=np.r_[ts[0]-previous,d]
  assert np.all(d>=0),'source timestamps not monotonic after timezone conversion'
  equal+=int(np.count_nonzero(d==0));maximum=max(maximum,int(d.max()) if len(d) else 0)
  if first is None:first=int(ts[0])
  previous=int(ts[-1]);count+=len(ts)
 with parquet.open('rb') as f:parquet_hash=hashlib.file_digest(f,'sha256').hexdigest()
 identity=json.dumps({'parquet':parquet_hash,'spec':spec.model_dump(),'data_kind':'ticks'},sort_keys=True).encode()
 epoch=datetime(1970,1,1,tzinfo=timezone.utc)
 manifest=TickManifest(dataset_id=hashlib.sha256(identity).hexdigest(),spec=spec,profile=profile,source_file='DEUIDXEUR.csv.gz',source_sha256=expected_gzip,parquet_sha256=parquet_hash,created_at=datetime.now(timezone.utc),rows=count,first_timestamp=epoch+timedelta(microseconds=first),last_timestamp=epoch+timedelta(microseconds=previous),equal_timestamp_records=equal,max_interval_seconds=maximum/1e6)
 output.mkdir();parquet.rename(output/'ticks.parquet')
 (output/'manifest.json').write_text(manifest.model_dump_json(indent=2)+'\n')
 audit={'compressed_sha256':expected_gzip,**raw,'rows':count,'missing_volume':'not exported; stored as zero','quote_rows_dropped':0,'timezone':'Europe/Kyiv','dst_ambiguity_rejected':True,'git_commit':'b0b20bc6e7eecb39e5c56e6623f711fd3fb3988d','elapsed_seconds':time.monotonic()-started}
 (output/'source-verification.json').write_text(json.dumps(audit,indent=2)+'\n')
 print(json.dumps({'stage':'import_completed','rows':count,'first':manifest.first_timestamp.isoformat(),'last':manifest.last_timestamp.isoformat(),'elapsed_seconds':time.monotonic()-started}),flush=True)
