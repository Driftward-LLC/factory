CREATE FUNCTION pg_temp.fingerprints() RETURNS TABLE(schema_name text,table_name text,row_count bigint,row_sha256 text) LANGUAGE plpgsql AS $$
DECLARE item record;
BEGIN
 FOR item IN SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','m') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' ORDER BY n.nspname,c.relname LOOP
  schema_name:=item.nspname; table_name:=item.relname;
  EXECUTE format($q$SELECT count(*),encode(sha256(convert_to(coalesce(string_agg(h,'' ORDER BY h COLLATE "C"),''),'UTF8')),'hex') FROM (SELECT encode(sha256(convert_to(row_to_json(t)::text,'UTF8')),'hex') h FROM %I.%I t) s$q$,schema_name,table_name) INTO row_count,row_sha256;
  RETURN NEXT;
 END LOOP;
END$$;
SELECT json_build_object(
 'tables',(SELECT coalesce(json_agg(t ORDER BY schema_name,table_name),'[]') FROM pg_temp.fingerprints() t),
 'columns',(SELECT coalesce(json_agg(t ORDER BY table_schema,table_name,ordinal_position),'[]') FROM (SELECT table_schema,table_name,column_name,ordinal_position,data_type,udt_name,is_nullable,column_default,collation_name FROM information_schema.columns WHERE table_schema NOT LIKE 'pg_%' AND table_schema<>'information_schema') t),
 'indexes',(SELECT coalesce(json_agg(t ORDER BY schemaname,tablename,indexname),'[]') FROM (SELECT schemaname,tablename,indexname,indexdef FROM pg_indexes WHERE schemaname NOT LIKE 'pg_%') t),
 'constraints',(SELECT coalesce(json_agg(t ORDER BY schema_name,table_name,conname),'[]') FROM (SELECT n.nspname schema_name,c.relname table_name,k.conname,k.contype,pg_get_constraintdef(k.oid) definition,k.convalidated FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%') t),
 'sequences',(SELECT coalesce(json_agg(t ORDER BY schemaname,sequencename),'[]') FROM pg_sequences t WHERE schemaname NOT LIKE 'pg_%'));
