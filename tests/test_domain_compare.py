"""Comparison fixtures use mocked model/HTTP calls; never live Hermes proof."""
import copy
import importlib
import importlib.util
import json
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from chip_model.pipeline.domain_discovery.budget import RunBudget
from chip_model.pipeline.domain_discovery.entities import EntityResolver
from chip_model.pipeline.domain_discovery.models import ModelService
from chip_model.pipeline.domain_discovery.pages import PageService
from chip_model.pipeline.domain_discovery.rules import freeze_bundle
from chip_model.pipeline.domain_discovery.store import RunStore, _file_hash

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'chip_model.pipeline.domain_discovery.compare'


@pytest.fixture
def comparison():
    assert importlib.util.find_spec(MODULE) is not None, 'immutable comparison module missing'
    return importlib.import_module(MODULE)


class Model:
    model_name = 'comparison-controlled-fixture'
    def __init__(self, facts):
        self.responses = [{'facts': facts}, *[{'verdict': 'accept', 'reason': 'Fixture exact evidence supported.'} for _ in facts]]
    def call_model(self, **kwargs):
        return copy.deepcopy(self.responses.pop(0))


class Response:
    status_code = 200
    headers = {'Content-Type': 'text/plain'}
    encoding = 'utf-8'
    def __init__(self, text, url):
        self.text, self.url = text, url
    def iter_content(self, **kwargs):
        yield self.text.encode()
    def close(self):
        pass


class Session:
    headers = {}
    def __init__(self, text):
        self.text = text
    def get(self, url, **kwargs):
        return Response(self.text, url)


@pytest.fixture
def factory(tmp_path):
    store = RunStore(tmp_path / 'runs', clock=lambda: 1000)
    source = tmp_path / 'business.db'
    with closing(sqlite3.connect(source)) as db, db:
        db.executescript((ROOT / 'tests/fixtures/domain_discovery/business_schema.sql').read_text(encoding='utf-8'))
        db.executescript('CREATE TABLE models (id TEXT); CREATE TABLE field_provenance (id INTEGER PRIMARY KEY, table_name TEXT,row_id TEXT,field_name TEXT,new_value TEXT,source_url TEXT,source_type TEXT,confidence TEXT,evidence_ref_json TEXT);')
        db.execute("INSERT INTO models VALUES ('org/Model-7B')")
        db.execute("INSERT INTO chips(vendor,chip_model,vram_gb,form_factor,tdp_w) VALUES ('Vendor A','X80 80GB SXM','80','SXM','400')")
    def create(run_id='run-a', changes=None, provenance=(), manifest=None):
        if changes:
            with closing(sqlite3.connect(source)) as db, db:
                for field, value in changes.items():
                    db.execute(f'UPDATE chips SET {field}=? WHERE id=1', (value,))
        if provenance:
            with closing(sqlite3.connect(source)) as db, db:
                for i, row in enumerate(provenance, 1):
                    row = {'id': i, 'table_name':'chips', 'row_id':'1', 'field_name':'vram_gb', 'new_value':'80', 'source_type':'community', 'confidence':'low', **row}
                    db.execute('INSERT INTO field_provenance VALUES (?,?,?,?,?,?,?,?,?)', tuple(row.get(k) for k in ('id','table_name','row_id','field_name','new_value','source_url','source_type','confidence','evidence_ref_json')))
        store.start('start-'+run_id, {'domain':'specs'}, run_id)
        gen = store.claim(run_id, 'worker', 100000)
        budget = RunBudget(store,run_id,gen,monotonic=lambda:1000,epoch=lambda:1000)
        bundle = store.run_path(run_id) / 'skill_bundle'
        frozen = freeze_bundle(ROOT / '.hermes-skills',bundle)
        store.ensure_baseline(run_id,gen,source,rule_bundle_hash=frozen['bundle_sha256'],manifest=manifest)
        return {'store':store,'id':run_id,'gen':gen,'budget':budget,'bundle':bundle,'source':source}
    return create


def capture(ctx, url='https://vendor.example/spec', text='Vendor A X80 80GB SXM GPU: 80 GB; 400 W; 1 TB/s; 1000 GB/s.'):
    pages = PageService(ctx['store'],ctx['id'],ctx['gen'],ctx['budget'],session=Session(text),resolver=lambda _:['93.184.216.34'],monotonic=lambda:1000,backoff=lambda _:None)
    result = pages.visit({'candidate_id':url,'url':url})
    snapshot = pages.read_snapshot(result['snapshot_id'])
    ctx['pages'] = pages
    return snapshot


def fact(snapshot, domain='specs', sku='X80 80GB SXM', field='vram_gb', value=80, unit='GB', **updates):
    row = {'domain_id':domain,'target_table':'chips','field_name':field,'entity_key':{'vendor':'Vendor A','chip_model':sku},'entity_hints':{'full_sku':sku,'entity_type':'board_sku'},'raw_value':value,'normalized_value':value,'unit':unit,'conversion':{'source_unit':unit,'factor':1,'basis':'decimal'},'scope':'board_sku','evidence_kind':'explicit','conditions':{},'evidence':{'snapshot_id':snapshot['snapshot_id'],'body_hash':snapshot['body_hash'],'quote':snapshot['text'],'start':0,'end':len(snapshot['text'])},'context_refs':[],'explanation':'Explicit fixture SKU field.','source_type':'official_datasheet','confidence':'high','confidence_reason':'Exact fixture statement.'}
    row.update(updates)
    return row


def audit(ctx, snapshot, facts, scope='main'):
    model = ModelService(ctx['store'],ctx['id'],ctx['gen'],ctx['budget'],ctx['pages'],ctx['bundle'],domain_id=facts[0]['domain_id'],task_scope=scope,client=Model(facts),backoff=lambda _:None)
    return model.extract_page(snapshot['snapshot_id'])['candidates']


def identity(ctx, snapshot, sku='X80 80GB SXM'):
    row = audit(ctx,snapshot,[fact(snapshot,'identity',sku,'chip_type','GPU',None)])[0]
    EntityResolver(ctx['store'],ctx['id'],ctx['gen']).register_identity(row['logical_candidate_id'])


def service(comparison, ctx):
    return comparison.ComparisonService(ctx['store'],ctx['id'],ctx['gen'],pages=ctx['pages'])


@pytest.mark.parametrize('membership,quality,expected', [('added','upgraded','upgraded'),('added','unknown','added'),('added','conflict','conflict'),('unchanged','unknown','unknown'),('unchanged','unchanged','unchanged')])
def test_independent_source_summary_priority(comparison,membership,quality,expected):
    assert comparison.source_summary(membership,quality)==expected


@pytest.mark.parametrize('sku,old,new,kind,basis', [('X90 80GB SXM','80',80,'new_entity','entity_absent'),('X80 80GB SXM',None,80,'added','field_empty'),('X80 80GB SXM','64',80,'changed','frozen_value'),('X80 80GB SXM','80',80,'unchanged','frozen_value')])
def test_value_kinds_and_saved_baseline(comparison,factory,sku,old,new,kind,basis):
    ctx=factory(changes={'vram_gb':old})
    snapshot=capture(ctx,text=f'Vendor A {sku} GPU: 80 GB.')
    identity(ctx,snapshot,sku)
    row=audit(ctx,snapshot,[fact(snapshot,sku=sku,value=new)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['value_change_kind']==kind
    assert result['old_value_basis']==basis
    assert result['old_value']==(None if kind=='new_entity' else old)
    assert result['baseline_sha256']==ctx['store'].metadata(ctx['id'],'baseline')['baseline_sha256']
    assert result['baseline_time']==1000
    assert result['formal_business_writes']==result['formal_provenance_writes']==0


def test_explicit_conversion_and_original_values_preserved(comparison,factory):
    ctx=factory(changes={'vram_bw_gb_s':'1000'})
    snapshot=capture(ctx)
    identity(ctx,snapshot)
    row=audit(ctx,snapshot,[fact(snapshot,field='vram_bw_gb_s',value=1000,raw_value=1,unit='GB/s',conversion={'source_unit':'TB/s','factor':1000,'basis':'decimal','direction':'unidirectional','aggregation':'per_chip'})])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['value_change_kind']=='unchanged'
    assert result['candidate']['raw_value']==1 and result['candidate']['conversion']['factor']==1000
    assert result['candidate']['conversion']['direction']=='unidirectional'


def test_only_provenance_supporting_frozen_value_counts_and_no_source_cap(comparison,factory):
    provenance=[{'source_url':f'https://old.example/{i}','new_value':'80.0'} for i in range(7)]
    provenance.append({'source_url':'https://irrelevant.example/','new_value':'32'})
    ctx=factory(provenance=provenance)
    snapshot=capture(ctx,url='https://old.example/6')
    identity(ctx,snapshot)
    row=audit(ctx,snapshot,[fact(snapshot)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert len(result['old_sources'])==7
    assert len(result['old_source_references'])==8
    assert result['source_membership_kind']=='unchanged'
    assert result['source_quality_kind']=='unknown'
    assert result['source_change_kind']=='unknown'
    assert 'archived' in result['source_quality_reason']


def test_missing_old_evidence_is_unknown_not_official_upgrade(comparison,factory):
    ctx=factory(provenance=[{'source_url':'https://old.example/spec'}])
    snapshot=capture(ctx)
    identity(ctx,snapshot)
    row=audit(ctx,snapshot,[fact(snapshot)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['source_membership_kind']=='added'
    assert result['source_quality_kind']=='unknown'
    assert result['source_change_kind']=='added'


def archived(factory, *, quote=None, value=80, conversion=None):
    old=factory('old-run')
    snapshot=capture(old,url='https://old.example/spec',text=f'Vendor A X80 80GB SXM GPU; X80: {value} GB.')
    identity(old,snapshot)
    candidate=fact(snapshot,value=value)
    if quote:
        start=snapshot['text'].index(quote)
        candidate['evidence'].update(quote=quote,start=start,end=start+len(quote))
    if conversion:
        candidate['conversion']=conversion
    row=audit(old,snapshot,[candidate])[0]
    EntityResolver(old['store'],old['id'],old['gen']).bind_candidate(row['logical_candidate_id'])
    ref={'run_id':old['id'],'logical_candidate_id':row['logical_candidate_id'],'version':0}
    return old,ref


def test_actual_archived_context_support_can_upgrade_and_still_add(comparison,factory):
    _,ref=archived(factory,quote='X80: 80 GB.')
    ctx=factory('new-run',provenance=[{'source_url':'https://old.example/spec','evidence_ref_json':json.dumps(ref)}])
    snapshot=capture(ctx)
    identity(ctx,snapshot)
    row=audit(ctx,snapshot,[fact(snapshot)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['source_membership_kind']=='added'
    assert result['source_quality_kind']=='upgraded'
    assert result['source_change_kind']=='upgraded'
    assert result['old_sources'][0]['verified_evidence']['body_hash']


def test_actual_comparable_evidence_conflict_keeps_both(comparison,factory):
    _,ref=archived(factory)
    ctx=factory('new-run',provenance=[{'source_url':'https://old.example/spec','evidence_ref_json':json.dumps(ref)}])
    snapshot=capture(ctx,text='Vendor A X80 80GB SXM GPU: 96 GB.')
    identity(ctx,snapshot)
    row=audit(ctx,snapshot,[fact(snapshot,value=96)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['value_change_kind']=='changed'
    assert result['source_quality_kind']=='conflict'
    assert result['source_change_kind']=='conflict'
    assert result['old_value']=='80' and result['candidate']['normalized_value']==96
    assert not result['publish_eligible']


def test_same_url_actual_supported_body_version_is_added(comparison,factory):
    _,ref=archived(factory)
    ctx=factory('new-run',provenance=[{'source_url':'https://old.example/spec','evidence_ref_json':json.dumps(ref)}])
    snapshot=capture(ctx,url='https://old.example/spec',text='Vendor A X80 80GB SXM GPU: 80 GB; revised document.')
    identity(ctx,snapshot)
    row=audit(ctx,snapshot,[fact(snapshot)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['source_membership_kind']=='added'
    assert 'version' in result['source_membership_reason']


def test_fake_archived_verified_flag_is_not_trusted(comparison,factory):
    ref={'run_id':'missing','logical_candidate_id':'fake','verified':True}
    ctx=factory(provenance=[{'source_url':'https://old.example/spec','evidence_ref_json':json.dumps(ref)}])
    snapshot=capture(ctx)
    identity(ctx,snapshot)
    row=audit(ctx,snapshot,[fact(snapshot)])[0]
    assert service(comparison,ctx).compare_candidate(row['logical_candidate_id'])['source_quality_kind']=='unknown'


def test_comparison_replay_and_multi_source_field_stats(comparison,factory):
    ctx=factory()
    snap=capture(ctx)
    identity(ctx,snap)
    first=audit(ctx,snap,[fact(snap)])[0]
    compare=service(comparison,ctx)
    result=compare.compare_candidate(first['logical_candidate_id'])
    before=compare.snapshot()
    assert compare.compare_candidate(first['logical_candidate_id'])==result and compare.snapshot()==before
    snap2=capture(ctx,url='https://other.example/spec')
    second=audit(ctx,snap2,[fact(snap2)],scope='linked')[0]
    compare.compare_candidate(second['logical_candidate_id'])
    stats=compare.statistics()
    assert stats['independent_fields']==1 and stats['source_candidates']==2 and stats['urls']==2
    assert stats['main']['independent_fields']==stats['linked']['independent_fields']==1
    assert stats['new_entities']==0
    assert stats['formal_business_writes']==stats['formal_provenance_writes']==0


def test_history_reads_saved_old_value_only(comparison,factory):
    ctx=factory()
    snap=capture(ctx)
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap)])[0]
    compare=service(comparison,ctx)
    result=compare.compare_candidate(row['logical_candidate_id'])
    with closing(sqlite3.connect(ctx['source'])) as db,db:
        db.execute("UPDATE chips SET vram_gb='999'")
    history=importlib.import_module('chip_model.pipeline.domain_discovery.history')
    assert history.project_comparison(result)['old_value']=='80'
    missing=history.project_comparison({'candidate':result['candidate']})
    assert missing['old_value'] is None and missing['old_value_basis']=='old_value_not_recorded'
    assert missing['value_change_kind']=='unknown'
    assert compare.compare_candidate(row['logical_candidate_id'])==result


def test_frozen_baseline_tampering_fails_closed(comparison,factory):
    ctx=factory()
    snap=capture(ctx)
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap)])[0]
    compare=service(comparison,ctx)
    path=ctx['store'].run_path(ctx['id'])/'data.db'
    path.chmod(stat.S_IWRITE)
    path.write_bytes(path.read_bytes()+b'tamper')
    with pytest.raises(ValueError,match='baseline'):
        compare.compare_candidate(row['logical_candidate_id'])

def test_relation_fields_use_full_conditions_and_never_count_new_chips(comparison,factory):
    ctx=factory()
    snap=capture(ctx,text='Vendor A X80 80GB SXM GPU; org/Model-7B suite-A inference 2 cards BF16 config-A batch 4 batch 8 input 128 output 256: 100 tokens/s.')
    identity(ctx,snap)
    def relation(batch):
        return fact(snap,'benchmark',field='throughput_tok_s',value=100,unit='tokens/s',target_table='chip_model_benchmarks',scope='relation',evidence_kind='measured',entity_hints={'vendor':'Vendor A','full_sku':'X80 80GB SXM','entity_type':'board_sku'},entity_key={'chip_model':'X80 80GB SXM','model_id':'org/Model-7B','suite_name':'suite-A','workload_type':'inference','chip_count':2,'precision':'BF16'},conditions={'hardware_config':'config-A','metric_class':'generative_throughput','batch_size':batch,'input_seq_length':128,'output_seq_length':256})
    first=audit(ctx,snap,[relation(4)])[0]
    snap=capture(ctx,url='https://other.example/spec',text=snap['text'])
    second=audit(ctx,snap,[relation(8)])[0]
    compare=service(comparison,ctx)
    results=[compare.compare_candidate(row['logical_candidate_id']) for row in (first,second)]
    assert all(r['validated'] and r['old_value_basis']=='relation_absent' and r['old_value'] is None for r in results)
    stats=compare.statistics()
    assert stats['independent_fields']==2 and stats['source_candidates']==stats['urls']==2
    assert stats['new_entities']==0


def test_stats_rebind_ambiguous_entities_and_exclude_incomplete_benchmarks(comparison,factory):
    ctx=factory()
    snap=capture(ctx,text='Vendor A X90 GPU; X90 64GB PCIe GPU; X90 80GB SXM GPU; 64 GB; org/Model-7B 100 tokens/s BF16 2 cards suite-A config-A.')
    identity(ctx,snap,'X90')
    first=audit(ctx,snap,[fact(snap,sku='X90',value=64)])[0]
    compare=service(comparison,ctx)
    early=compare.compare_candidate(first['logical_candidate_id'])
    assert early['validated']
    variants=capture(ctx,url='https://vendor.example/variants',text=snap['text'])
    rows=audit(ctx,variants,[fact(variants,'identity','X90','chip_type','GPU',None,entity_hints={'full_sku':'X90','entity_type':'board_sku','capacity':capacity,'form_factor':form}) for capacity,form in [('64GB','PCIe'),('80GB','SXM')]])
    for row in rows:
        EntityResolver(ctx['store'],ctx['id'],ctx['gen']).register_identity(row['logical_candidate_id'])
    # Saved old binding remains intact, final current binding is ambiguous.
    stats=compare.statistics()
    assert stats['independent_fields']==0 and stats['validated_candidates']==0
    assert stats['new_entities']==2
    assert compare.snapshot()['comparisons'][first['logical_candidate_id']][0]==early


def test_rejected_candidates_do_not_contribute_fields_or_sources(comparison,factory):
    ctx=factory()
    snap=capture(ctx)
    identity(ctx,snap)
    rows=audit(ctx,snap,[fact(snap,unit='not_a_unit')])
    assert rows[0]['status']=='program_rejected'
    compare=service(comparison,ctx)
    result=compare.compare_candidate(rows[0]['logical_candidate_id'])
    assert not result['validated'] and result['value_change_kind']=='unknown'
    stats=compare.statistics()
    assert stats['independent_fields']==stats['source_candidates']==0
    assert stats['rejected_candidates']==1


def test_missing_minimum_benchmark_is_not_validated_in_stats(comparison,factory):
    ctx=factory()
    snap=capture(ctx,text='Vendor A X80 80GB SXM GPU; org/Model-7B suite-A inference 2 cards BF16 config-A: 100 tokens/s.')
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap,'benchmark',field='throughput_tok_s',value=100,unit='tokens/s',target_table='chip_model_benchmarks',scope='relation',evidence_kind='measured',entity_hints={'vendor':'Vendor A','full_sku':'X80 80GB SXM','entity_type':'board_sku'},entity_key={'chip_model':'X80 80GB SXM','model_id':'org/Model-7B','suite_name':'suite-A','workload_type':'inference','chip_count':2,'precision':'BF16'},conditions={'hardware_config':'config-A','metric_class':'generative_throughput'})])[0]
    compare=service(comparison,ctx)
    result=compare.compare_candidate(row['logical_candidate_id'])
    assert not result['validated']
    assert compare.statistics()['validated_candidates']==0


def test_archive_mismatch_and_corrupt_snapshot_do_not_upgrade(comparison,factory):
    old,ref=archived(factory,quote='X80: 80 GB.')
    ctx=factory('new-run',provenance=[{'source_url':'https://old.example/spec','evidence_ref_json':json.dumps(ref)}])
    snap=capture(ctx)
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap)])[0]
    archive=old['store'].metadata(old['id'],ModelService.KEY)['candidates'][ref['logical_candidate_id']]
    record=old['store'].metadata(old['id'],PageService.KEY)['snapshots'][archive['versions'][0]['candidate']['evidence']['snapshot_id']]
    old['store'].run_path(old['id']).joinpath('source_snapshots',record['text_ref']).write_bytes(b'corrupt')
    compare=service(comparison,ctx)
    result=compare.compare_candidate(row['logical_candidate_id'])
    assert result['source_quality_kind']=='unknown'
    assert 'hash' in result['old_sources'][0]['evidence_unavailable_reason']

def test_archive_requires_exact_original_model_version(comparison,factory):
    _,ref=archived(factory,quote='X80: 80 GB.')
    ref['version']=999
    ctx=factory('new-run',provenance=[{'source_url':'https://old.example/spec','evidence_ref_json':json.dumps(ref)}])
    snap=capture(ctx)
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['source_quality_kind']=='unknown'
    assert 'version' in result['old_sources'][0]['evidence_unavailable_reason']


def test_archive_verification_does_not_write_old_run(comparison,factory):
    old,ref=archived(factory,quote='X80: 80 GB.')
    ctx=factory('new-run',provenance=[{'source_url':'https://old.example/spec','evidence_ref_json':json.dumps(ref)}])
    snap=capture(ctx)
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap)])[0]
    before={key:old['store'].metadata(old['id'],key) for key in (ModelService.KEY,EntityResolver.KEY,PageService.KEY,'baseline')}
    events=old['store'].events(old['id'])
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['source_quality_kind']=='upgraded'
    assert before=={key:old['store'].metadata(old['id'],key) for key in before}
    assert events==old['store'].events(old['id'])

def test_field_count_uses_canonical_entity_not_original_hint_spelling(comparison,factory):
    ctx=factory()
    snap=capture(ctx)
    identity(ctx,snap)
    first=audit(ctx,snap,[fact(snap)])[0]
    compare=service(comparison,ctx)
    compare.compare_candidate(first['logical_candidate_id'])
    snap=capture(ctx,url='https://other.example/spec')
    second=audit(ctx,snap,[fact(snap,entity_key={'vendor':'VENDOR A','chip_model':'x80 80gb sxm'},entity_hints={'full_sku':'x80 80gb sxm','entity_type':'board_sku'})])[0]
    compare.compare_candidate(second['logical_candidate_id'])
    assert compare.statistics()['independent_fields']==1

def test_frozen_empty_source_membership_adds_actual_new_url(comparison,factory):
    ctx=factory()
    snap=capture(ctx)
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['source_membership_kind']=='added'
    assert result['source_quality_kind']=='unknown'

def test_manifest_archive_refs_work_without_extra_formal_provenance_column(comparison,factory):
    old,ref=archived(factory,quote='X80: 80 GB.')
    with closing(sqlite3.connect(old['source'])) as db,db:
        db.execute('ALTER TABLE field_provenance DROP COLUMN evidence_ref_json')
        db.execute("INSERT INTO field_provenance(id,table_name,row_id,field_name,new_value,source_url,source_type,confidence) VALUES (1,'chips','1','vram_gb','80','https://old.example/spec','community','low')")
    ctx=factory('new-run',manifest={'historical_evidence_refs':{'1':ref}})
    snap=capture(ctx)
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap)])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['source_quality_kind']=='upgraded'
    assert 'evidence_ref_json' not in result['old_sources'][0]
    assert result['old_sources'][0]['verified_evidence']['model_proof']['version']==0

def test_raw_unbindable_rejections_count_without_fake_fields(comparison,factory):
    ctx=factory()
    snap=capture(ctx)
    model=ModelService(ctx['store'],ctx['id'],ctx['gen'],ctx['budget'],ctx['pages'],ctx['bundle'],domain_id='specs',client=Model([123]),backoff=lambda _:None)
    rows=model.extract_page(snap['snapshot_id'])['candidates']
    assert rows[0]['status']=='program_rejected'
    stats=service(comparison,ctx).statistics()
    assert stats['rejected_candidates']==1
    assert stats['independent_fields']==stats['source_candidates']==stats['new_entities']==0

@pytest.mark.parametrize('shape',['scalar','unowned'])
def test_unbindable_input_has_typed_skip_not_blanket_error_catching(comparison,factory,shape):
    ctx=factory()
    snap=capture(ctx)
    value=123 if shape=='scalar' else fact(snap,field='price_usd')
    model=ModelService(ctx['store'],ctx['id'],ctx['gen'],ctx['budget'],ctx['pages'],ctx['bundle'],domain_id='specs',client=Model([value]),backoff=lambda _:None)
    row=model.extract_page(snap['snapshot_id'])['candidates'][0]
    compare=service(comparison,ctx)
    outcome=compare.comparison_input(row['logical_candidate_id'])
    assert outcome['is_comparable'] is False and outcome['status']=='skipped'
    assert outcome['candidate_status']=='program_rejected'
    result=compare.compare_candidate(row['logical_candidate_id'])
    assert result==outcome
    assert 'entity' not in result and 'old_value' not in result
    assert compare.statistics()['rejected_candidates']==1

def test_support_completeness_does_not_infer_conditions_from_other_numbers(comparison):
    # Unit fixture for support dimensions only; authoritative production calls
    # first verify actual stored snapshot/review/entity references.
    candidate={'evidence':{'quote':'Vendor A X80 64GB GPU has batch size 8.'},'context_refs':[],
               'conditions':{'batch_size':4}}
    entity={'hint':{'vendor':'vendor a','full_sku':'x80'}}
    assert 'condition:batch_size' not in comparison._support(candidate,entity)
    entity={'hint':{'vendor':'vendor a','full_sku':'x8'}}
    assert 'full_sku' not in comparison._support(candidate,entity)

def test_one_sided_unknown_measurement_basis_is_unknown_not_conflict(comparison,factory):
    old=factory('old-run',changes={'vram_bw_gb_s':'1000'})
    snap=capture(old,url='https://old.example/bandwidth',text='Vendor A X80 80GB SXM GPU: 1000 GB/s bandwidth.')
    identity(old,snap)
    row=audit(old,snap,[fact(snap,field='vram_bw_gb_s',value=1000,unit='GB/s')])[0]
    EntityResolver(old['store'],old['id'],old['gen']).bind_candidate(row['logical_candidate_id'])
    ref={'run_id':old['id'],'logical_candidate_id':row['logical_candidate_id'],'version':0}
    ctx=factory('new-run',provenance=[{'field_name':'vram_bw_gb_s','new_value':'1000','source_url':'https://old.example/bandwidth','evidence_ref_json':json.dumps(ref)}])
    snap=capture(ctx,text='Vendor A X80 80GB SXM GPU: unidirectional 1000 GB/s bandwidth.')
    identity(ctx,snap)
    row=audit(ctx,snap,[fact(snap,field='vram_bw_gb_s',value=1000,unit='GB/s',conversion={'source_unit':'GB/s','factor':1,'basis':'decimal','direction':'unidirectional'})])[0]
    result=service(comparison,ctx).compare_candidate(row['logical_candidate_id'])
    assert result['source_quality_kind']=='unknown'
    assert result['candidate']['conversion']['direction']=='unidirectional'
