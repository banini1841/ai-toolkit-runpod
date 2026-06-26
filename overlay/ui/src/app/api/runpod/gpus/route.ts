import { NextResponse } from 'next/server';
import { PrismaClient } from '@prisma/client';

// RunPod extension — live GPU-type list for the job form's RunPod selector.
// Queries the RunPod GraphQL API with the stored key; falls back to a static
// curated list when no key is set or the call fails. New file → survives pulls.
const prisma = new PrismaClient();

export interface RunpodGpu {
  id: string; // value passed to create_pod as gpu_type_id
  label: string;
  memoryInGb: number;
  secure: boolean;
  community: boolean;
}

// Curated fallback. ids are RunPod gpuType.id values (display-name style).
const FALLBACK_GPUS: RunpodGpu[] = [
  { id: 'NVIDIA GeForce RTX 4090', label: 'RTX 4090 · 24GB', memoryInGb: 24, secure: true, community: true },
  { id: 'NVIDIA RTX 4000 Ada Generation', label: 'RTX 4000 Ada · 20GB', memoryInGb: 20, secure: true, community: true },
  { id: 'NVIDIA RTX A5000', label: 'RTX A5000 · 24GB', memoryInGb: 24, secure: true, community: true },
  { id: 'NVIDIA GeForce RTX 3090', label: 'RTX 3090 · 24GB', memoryInGb: 24, secure: true, community: true },
  { id: 'NVIDIA RTX A6000', label: 'RTX A6000 · 48GB', memoryInGb: 48, secure: true, community: true },
  { id: 'NVIDIA RTX 6000 Ada Generation', label: 'RTX 6000 Ada · 48GB', memoryInGb: 48, secure: true, community: true },
  { id: 'NVIDIA L40', label: 'L40 · 48GB', memoryInGb: 48, secure: true, community: true },
  { id: 'NVIDIA L40S', label: 'L40S · 48GB', memoryInGb: 48, secure: true, community: true },
  { id: 'NVIDIA A100 80GB PCIe', label: 'A100 80GB PCIe · 80GB', memoryInGb: 80, secure: true, community: true },
  { id: 'NVIDIA A100-SXM4-80GB', label: 'A100 SXM4 · 80GB', memoryInGb: 80, secure: true, community: true },
  { id: 'NVIDIA H100 PCIe', label: 'H100 PCIe · 80GB', memoryInGb: 80, secure: true, community: true },
  { id: 'NVIDIA H100 80GB HBM3', label: 'H100 SXM · 80GB', memoryInGb: 80, secure: true, community: true },
];

async function getApiKey(): Promise<string> {
  const row = await prisma.settings.findFirst({ where: { key: 'RUNPOD_API_KEY' } });
  return row?.value && row.value !== '' ? row.value : '';
}

const GQL = `query GpuTypes {
  gpuTypes {
    id
    displayName
    memoryInGb
    secureCloud
    communityCloud
  }
}`;

export async function GET() {
  const apiKey = await getApiKey();
  if (!apiKey) {
    return NextResponse.json({ source: 'fallback', gpus: FALLBACK_GPUS, hasApiKey: false });
  }

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    const res = await fetch(`https://api.runpod.io/graphql?api_key=${encodeURIComponent(apiKey)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: GQL }),
      cache: 'no-store',
      signal: controller.signal,
    });
    clearTimeout(timeout);

    if (!res.ok) {
      return NextResponse.json({ source: 'fallback', gpus: FALLBACK_GPUS, hasApiKey: true, error: `RunPod responded ${res.status}` });
    }
    const data = await res.json();
    const list: any[] = data?.data?.gpuTypes ?? [];
    if (!Array.isArray(list) || list.length === 0) {
      return NextResponse.json({ source: 'fallback', gpus: FALLBACK_GPUS, hasApiKey: true });
    }
    const gpus: RunpodGpu[] = list
      .map(g => ({
        id: g.id as string,
        label: `${g.displayName ?? g.id}${g.memoryInGb ? ` · ${g.memoryInGb}GB` : ''}`,
        memoryInGb: Number(g.memoryInGb ?? 0),
        secure: !!g.secureCloud,
        community: !!g.communityCloud,
      }))
      .sort((a, b) => a.memoryInGb - b.memoryInGb);
    return NextResponse.json({ source: 'runpod', gpus, hasApiKey: true });
  } catch (error) {
    return NextResponse.json({
      source: 'fallback',
      gpus: FALLBACK_GPUS,
      hasApiKey: true,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}
