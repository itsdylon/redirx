"""Explicit historical pages plus full-list compatibility for older callers."""
import base64
from datetime import datetime, timezone
import json
import re
from uuid import UUID


class InvalidHistoryPage(ValueError):
    pass


def _uuid(value):
    if not isinstance(value,str) or str(UUID(value)) != value:
        raise ValueError('Invalid history identifier')
    return value


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value,str) or len(value)>64:
        raise ValueError('Invalid history timestamp')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        # The live legacy migration_sessions.created_at column is timestamp
        # without time zone, populated by now() in the UTC database. PostgREST
        # preserves that shape. Normalize for comparisons; retain raw row text
        # and cursor precision so existing timezone-aware schemas still work.
        parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_session_page(client,owner,*,search='',cursor=None,limit=50):
    try:
        _uuid(owner)
        if not isinstance(search,str) or len(search)>200 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('Invalid history query')
        search=search.strip()
        after=None
        if cursor is not None:
            if not isinstance(cursor,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,2048}',cursor):
                raise ValueError('Invalid history cursor')
            after=json.loads(base64.b64decode(cursor+'='*(-len(cursor)%4),altchars=b'-_',validate=True))
            if not isinstance(after,dict) or set(after)!={'v','owner','search','created_at','id'} or type(after['v']) is not int or after['v']!=1:
                raise ValueError('Invalid history cursor')
            if after['owner']!=owner or after['search']!=search:
                raise ValueError('History cursor does not match this account or search')
            _uuid(after['id']);_timestamp(after['created_at'])
    except (ValueError,TypeError,KeyError,UnicodeError) as exc:
        raise InvalidHistoryPage('Invalid history page. Start again from the first page.') from exc
    response=client.rpc('legacy_session_page',{'p_user_id':owner,'p_search':search,
        'p_after_created_at':after['created_at'] if after else None,
        'p_after_id':after['id'] if after else None,'p_limit':limit}).execute()
    data=getattr(response,'data',None)
    if getattr(response,'error',None) or not isinstance(data,dict) or data.get('user_id')!=owner or data.get('search')!=search:
        raise RuntimeError('History page unavailable')
    rows=data.get('sessions');count=data.get('total_count')
    if not isinstance(rows,list) or len(rows)>limit or type(count) is not int or count<len(rows) or 'next_after' not in data:
        raise RuntimeError('History page incomplete')
    fields={'id','project_name','created_at','total_mappings','approved_mappings','status','pipeline_type','source_session_id'}
    prior=after
    for row in rows:
        if not isinstance(row,dict) or not fields.issubset(row):
            raise RuntimeError('History row incomplete')
        _uuid(row['id']);stamp=_timestamp(row['created_at'])
        if prior:
            previous=_timestamp(prior['created_at'])
            progresses=(previous is None and stamp is not None) or (
                (previous is None and stamp is None and row['id']<prior['id']) or
                (previous is not None and stamp is not None and (stamp,row['id'])<(previous,prior['id'])))
            if not progresses:raise RuntimeError('History cursor did not progress')
        prior=row
    tail=data['next_after'];next_cursor=None
    if tail is not None:
        if not rows or len(rows)!=limit or not isinstance(tail,dict) or set(tail)!={'id','created_at'} or tail!={key:rows[-1][key] for key in ('id','created_at')}:
            raise RuntimeError('History continuation incomplete')
        payload={'v':1,'owner':owner,'search':search,**tail}
        next_cursor=base64.urlsafe_b64encode(json.dumps(payload,separators=(',',':'),ensure_ascii=False).encode()).decode().rstrip('=')
    return {'success':True,'pagination':'cursor','sessions':rows,'total_count':count,'next_cursor':next_cursor,'search':search}


def read_all_sessions(client,owner):
    """Legacy shape/extras preserved; bounded DB reads, unbounded response.

    ID progress and an initial upper bound prevent repeated/capped pages from
    silently truncating history or chasing later high-ID inserts indefinitely.
    This compatibility traversal is live, not a cross-request snapshot.
    """
    def query(columns='*'):
        return client.table('migration_sessions').select(columns).eq('user_id',owner).eq('is_tutorial',False).eq('is_preview',False)
    bound_response=query('id').order('id',desc=True).limit(1).execute()
    if getattr(bound_response,'error',None) or not isinstance(bound_response.data,list) or len(bound_response.data)>1:
        raise RuntimeError('History bound unavailable')
    if not bound_response.data:return {'success':True,'sessions':[]}
    bound=_uuid(bound_response.data[0].get('id'))
    rows=[];cursor=None
    while True:
        page=query().lte('id',bound).order('id').limit(500)
        if cursor:page=page.gt('id',cursor)
        response=page.execute();batch=response.data
        if getattr(response,'error',None) or not isinstance(batch,list) or len(batch)>500:
            raise RuntimeError('History page unavailable')
        if not batch:break
        for row in batch:
            if not isinstance(row,dict) or not {'id','user_id','created_at'}.issubset(row):raise RuntimeError('History row incomplete')
            row_id=_uuid(row.get('id'))
            if row.get('user_id')!=owner or row_id>bound or (cursor and row_id<=cursor):
                raise RuntimeError('History cursor did not progress')
            _timestamp(row.get('created_at'))
            rows.append(row);cursor=row_id
        if cursor==bound:break
    # Preserve the legacy descending timestamp convention (NULLS FIRST), adding
    # a deterministic raw-ID tie-break. No JS timestamp conversion is involved.
    floor=datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda row:(row.get('created_at') is None,_timestamp(row.get('created_at')) or floor,row['id']),reverse=True)
    return {'success':True,'sessions':rows}
