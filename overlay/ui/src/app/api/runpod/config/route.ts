import { NextResponse } from 'next/server';
import { PrismaClient } from '@prisma/client';

// RunPod extension — global credentials/config stored in the existing key/value
// Settings table (no schema change). New file, so it survives upstream git pulls.
const prisma = new PrismaClient();

export const RUNPOD_SETTING_KEYS = ['RUNPOD_API_KEY', 'RUNPOD_SSH_KEY_PATH'] as const;

const DEFAULTS: Record<string, string> = {
  RUNPOD_API_KEY: '',
  // Matches the standalone RunPod trainer default keypair.
  RUNPOD_SSH_KEY_PATH: '~/.ssh/id_ed25519',
};

export async function GET() {
  try {
    const rows = await prisma.settings.findMany({
      where: { key: { in: [...RUNPOD_SETTING_KEYS] } },
    });
    const out: Record<string, string> = { ...DEFAULTS };
    for (const row of rows) {
      if (row.value !== undefined && row.value !== null && row.value !== '') {
        out[row.key] = row.value;
      }
    }
    // Don't leak the raw key to the browser; report whether one is set instead.
    const hasApiKey = !!out.RUNPOD_API_KEY;
    return NextResponse.json({
      hasApiKey,
      RUNPOD_SSH_KEY_PATH: out.RUNPOD_SSH_KEY_PATH,
    });
  } catch (error) {
    return NextResponse.json({ error: 'Failed to fetch RunPod config' }, { status: 500 });
  }
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const ops: Promise<unknown>[] = [];
    for (const key of RUNPOD_SETTING_KEYS) {
      if (!(key in body)) continue;
      const value = String(body[key] ?? '');
      // Allow clearing the SSH path back to default, but ignore an empty API key
      // submit so we never wipe a saved key by accident.
      if (key === 'RUNPOD_API_KEY' && value === '') continue;
      ops.push(
        prisma.settings.upsert({
          where: { key },
          update: { value },
          create: { key, value },
        }),
      );
    }
    await Promise.all(ops);
    return NextResponse.json({ success: true });
  } catch (error) {
    return NextResponse.json({ error: 'Failed to update RunPod config' }, { status: 500 });
  }
}
