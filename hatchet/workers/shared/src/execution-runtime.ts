/** Bounded execution identity, never credentials, host paths or session material. */
export type ExecutionRuntimeBinding = {
  schema: 1;
  backend: string;
  kind: 'docker';
  instance_id: string;
  image_digest: string;
  incarnation: string;
};

export function executionRuntimeBinding(value: unknown): ExecutionRuntimeBinding {
  const v = value as ExecutionRuntimeBinding;
  if (!v || typeof v !== 'object' || Array.isArray(v)
      || Object.keys(v).sort().join(',') !== 'backend,image_digest,incarnation,instance_id,kind,schema'
      || v.schema !== 1 || v.kind !== 'docker' || typeof v.backend !== 'string'
      || !/^[a-z][a-z0-9_]{0,63}$/.test(v.backend)
      || typeof v.instance_id !== 'string' || typeof v.image_digest !== 'string' || typeof v.incarnation !== 'string'
      || !/^[a-f0-9]{64}$/.test(v.instance_id)
      || !/^sha256:[a-f0-9]{64}$/.test(v.image_digest)
      || !/^sha256:[a-f0-9]{64}$/.test(v.incarnation)) throw new Error('Invalid execution runtime binding');
  return { schema: 1, backend: v.backend, kind: 'docker', instance_id: v.instance_id,
    image_digest: v.image_digest, incarnation: v.incarnation };
}

export function sameExecutionRuntime(a: unknown, b: unknown): boolean {
  try { return JSON.stringify(executionRuntimeBinding(a)) === JSON.stringify(executionRuntimeBinding(b)); }
  catch { return false; }
}
