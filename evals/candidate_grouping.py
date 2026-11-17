"""Public grouped extraction corpus. Input loading never reads gold files.

Evaluation means a reserved partition, NOT a genuinely blind/independent Holdout.
Groups are frozen developer classifications; metadata does not prove absence of
semantic overlap. Supplier and byte/number-normalized duplicates are audited.
"""
from __future__ import annotations
import re
from pathlib import Path
from .candidate_quality import load_json, fingerprint, require

COUNTS={'Dev':30,'Validation':10,'Evaluation':20}

def corpus(root):
    from .candidate_quality import load_inputs
    root=Path(root)
    manifest=load_json(root/'manifest.json');extra=load_json(root/'inputs-additions.json')
    require(manifest.get('suite')=='candidate-extraction-grouped60-v1' and manifest.get('public_data') is True
            and manifest.get('independently_unseen') is False,'GROUPED_SCOPE_INVALID')
    require(fingerprint(extra)==manifest.get('input_additions_sha256'),'GROUPED_INPUT_DRIFT')
    base=load_inputs(root.parent/'candidate20')
    require(fingerprint(base)==extra.get('base_sha256'),'GROUPED_BASE_DRIFT')
    cases=base['cases']+extra['cases'];ids=[x['id'] for x in cases]
    require(len(cases)==60 and len(set(ids))==60,'GROUPED_CASES_INVALID')
    splits=manifest.get('partitions',{});groups=manifest.get('groups',{})
    require(set(splits)==set(COUNTS) and set(groups)==set(ids),'GROUPED_MEMBERSHIP_INVALID')
    all_ids=[];owners={};seen_text={};seen_shape={}
    by_id={x['id']:x for x in cases}
    for split,count in COUNTS.items():
        part=splits[split];require(isinstance(part,list) and len(part)==count,'GROUPED_SPLIT_SIZE')
        for id in part:
            require(id in by_id,'GROUPED_UNKNOWN_CASE');all_ids.append(id)
            group=groups[id];require(isinstance(group,str) and bool(group),'GROUP_INVALID')
            require(group not in owners or owners[group]==split,'CROSS_SPLIT_GROUP_LEAK');owners[group]=split
            source=by_id[id]['text']
            require(source not in seen_text,'DUPLICATE_SOURCE');seen_text[source]=split
            shape=re.sub(r'\d+(?:\.\d+)?','<number>',source.casefold())
            require(shape not in seen_shape or seen_shape[shape]==split,'CROSS_SPLIT_TEMPLATE_DUPLICATE')
            seen_shape[shape]=split
    require(sorted(all_ids)==sorted(ids),'GROUPED_DUPLICATE_OR_MISSING_CASE')
    return manifest,by_id


def load_partition(directory, *, gold=False):
    directory=Path(directory);desc=load_json(directory/('gold.json' if gold else 'inputs.json'))
    manifest,by_id=corpus(directory.parent);split=desc.get('split')
    require(split in COUNTS and desc.get('schema_version')==3 and desc.get('synthetic_only') is True,
            'GROUPED_DESCRIPTOR_INVALID')
    require(desc.get('manifest_sha256')==fingerprint(manifest),'GROUPED_MANIFEST_DRIFT')
    suite=f'candidate-extraction-grouped-{split.lower()}{COUNTS[split]}-v1'
    require(desc.get('suite')==suite,'GROUPED_SUITE_MISMATCH')
    if gold:
        from .candidate_quality import load_gold
        extra=load_json(directory.parent/'gold-additions.json')
        require(fingerprint(extra)==manifest.get('gold_additions_sha256'),'GROUPED_GOLD_DRIFT')
        base=load_gold(directory.parent.parent/'candidate20')
        require(fingerprint(base)==extra.get('base_gold_sha256'),'GROUPED_BASE_GOLD_DRIFT')
        answers=base['cases']+extra['cases']
        require(len(answers)==60 and len({x['id'] for x in answers})==60,'GROUPED_GOLD_COUNT')
        require({x['id'] for x in answers}==set(by_id),'GROUPED_GOLD_IDS')
        by_id={x['id']:x for x in answers}
    return {'schema_version':1,'suite':suite,'split':split,'synthetic_only':True,
        'public_data':True,'independently_unseen':False,'review_status':'developer_authored_public_not_independently_reviewed',
        'group_manifest_sha256':fingerprint(manifest),
        'cases':[by_id[id] for id in manifest['partitions'][split]]}


def audit(root):
    from .candidate_methods import validate_gold
    from .candidate_quality import load_gold
    root=Path(root);manifest,_=corpus(root);vendors={};results={}
    for split in COUNTS:
        directory=root/split.lower();results[split]=validate_gold(directory)
        for case in load_gold(directory)['cases']:
            vendor=case['values'].get('supplier_id')
            if vendor is not None:
                key=vendor.strip().casefold()
                require(key not in vendors or vendors[key]==split,'CROSS_SPLIT_SUPPLIER_LEAK')
                vendors[key]=split
    return {'status':'passed','tasks':60,'partitions':COUNTS,'groups':len(set(manifest['groups'].values())),
        'manifest_sha256':fingerprint(manifest),'checks':['membership','source_hash','declared_groups',
            'exact_source_duplicates','number_normalized_duplicates','supplier_identity_overlap','gold_structure'],
        'independently_unseen':False,'gold_independently_reviewed':False,'model_quality_measured':False,
        'scope':'public synthetic extraction-only partitions; no generalization or full procurement claim',
        'partition_records':results}
