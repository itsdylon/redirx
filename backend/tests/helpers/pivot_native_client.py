"""PostgREST-shaped transport for disposable native PostgreSQL acceptance.

Only transport is adapted. Real services, SQL functions, grants, ownership
filters, constraints and persisted rows execute unchanged under service_role.
"""
from datetime import datetime, date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID


def json_value(value):
    if isinstance(value, (UUID, datetime, date)): return str(value)
    if isinstance(value, Decimal): return float(value)
    if isinstance(value, list): return [json_value(item) for item in value]
    if isinstance(value, dict): return {key: json_value(item) for key, item in value.items()}
    return value


class NativeClient:
    def __init__(self, dsn): self.dsn = dsn
    def table(self, name): return NativeQuery(self, table=name)
    def rpc(self, name, params): return NativeQuery(self, rpc=name, params=params)


class NativeQuery:
    def __init__(self, client, table=None, rpc=None, params=None):
        self.client, self.table_name, self.rpc_name, self.params = client, table, rpc, params
        self.columns='*'; self.filters=[]; self.orders=[]; self.max_rows=None; self.offset=0
        self.payload=None; self.mode='select'; self.one=False
    def select(self, columns='*', **kwargs): self.columns=columns; return self
    def eq(self, key, value): self.filters.append((key,'=',value)); return self
    def gt(self, key, value): self.filters.append((key,'>',value)); return self
    def is_(self, key, value):
        if value != 'null': raise ValueError('Native fixture supports only IS NULL')
        self.filters.append((key,'IS NULL',None)); return self
    def in_(self, key, value): self.filters.append((key,'IN',value)); return self
    def order(self, key, desc=False): self.orders.append((key,desc)); return self
    def limit(self, count): self.max_rows=count; return self
    def range(self, first, last): self.offset=first; self.max_rows=last-first+1; return self
    def maybe_single(self): self.one=True; return self
    def single(self): self.one=True; return self
    def insert(self, value): self.payload=value; self.mode='insert'; return self
    def update(self, value): self.payload=value; self.mode='update'; return self

    def execute(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        def bind(value): return Jsonb(value) if isinstance(value,(dict,list)) else value
        def column(name):
            if '->>' in name:
                key,path=name.split('->>',1)
                return sql.SQL('{}->>{}').format(sql.Identifier(key),sql.Literal(path))
            return sql.Identifier(name)
        args=[]
        with psycopg.connect(self.client.dsn,row_factory=dict_row) as conn:
            conn.execute('SET ROLE service_role')
            try:
                if self.rpc_name:
                    args=[bind(value) for value in self.params.values()]
                    statement=sql.SQL('SELECT {}({}) AS result').format(sql.Identifier(self.rpc_name),sql.SQL(',').join(
                        sql.SQL('{} => %s').format(sql.Identifier(key)) for key in self.params))
                    data=conn.execute(statement,args).fetchone()['result']
                else:
                    fields=sql.SQL('*') if self.columns=='*' else sql.SQL(',').join(column(key.strip()) for key in self.columns.split(','))
                    table=sql.Identifier(self.table_name)
                    if self.mode=='insert':
                        values=self.payload if isinstance(self.payload,list) else [self.payload]
                        keys=list(values[0]);args=[bind(row[key]) for row in values for key in keys]
                        statement=sql.SQL('INSERT INTO {} ({}) VALUES {} RETURNING *').format(table,sql.SQL(',').join(map(sql.Identifier,keys)),
                            sql.SQL(',').join(sql.SQL('({})').format(sql.SQL(',').join(sql.Placeholder() for _ in keys)) for _ in values))
                    else:
                        if self.mode=='update':
                            args=[bind(value) for value in self.payload.values()]
                            statement=sql.SQL('UPDATE {} SET {}').format(table,sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in self.payload))
                        else: statement=sql.SQL('SELECT {} FROM {}').format(fields,table)
                        clauses=[]
                        for key,operator,value in self.filters:
                            if operator=='IS NULL':
                                clauses.append(sql.SQL('{} IS NULL').format(column(key)))
                                continue
                            if operator=='IN': clauses.append(sql.SQL('{}=ANY(%s)').format(column(key)))
                            else: clauses.append(sql.SQL('{} {} %s').format(column(key),sql.SQL(operator)))
                            args.append(value)
                        if clauses: statement+=sql.SQL(' WHERE ')+sql.SQL(' AND ').join(clauses)
                        if self.mode=='update': statement+=sql.SQL(' RETURNING *')
                        else:
                            if self.orders: statement+=sql.SQL(' ORDER BY ')+sql.SQL(',').join(column(key)+sql.SQL(' DESC' if desc else ' ASC') for key,desc in self.orders)
                            if self.max_rows is not None: statement+=sql.SQL(' LIMIT %s');args.append(self.max_rows)
                            if self.offset: statement+=sql.SQL(' OFFSET %s');args.append(self.offset)
                    data=conn.execute(statement,args).fetchall()
                    if self.one: data=data[0] if data else None
            except psycopg.Error as exc:
                exc.code,exc.message=exc.sqlstate,exc.diag.message_primary
                raise
        return SimpleNamespace(data=json_value(data),error=None)
