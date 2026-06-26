'use client';

// RunPod extension — self-contained form section for the new-job form.
// Adds a "train remotely on RunPod" switch + GPU/cloud selectors and inline
// credential entry. New file, so it survives upstream git pulls; the only edit
// to a tracked file is a 2-line render of <RunpodSection/> in SimpleJob.tsx.

import { useEffect, useRef, useState } from 'react';
import { JobConfig } from '@/types';
import { apiClient } from '@/utils/api';
import { Checkbox, SelectInput, NumberInput, TextInput } from '@/components/formInputs';
import Card from '@/components/Card';

interface RunpodGpu {
  id: string;
  label: string;
  memoryInGb: number;
  secure: boolean;
  community: boolean;
}

export const DEFAULT_RUNPOD = {
  enabled: false,
  gpu_type: 'NVIDIA GeForce RTX 4090',
  cloud_type: 'SECURE',
  container_disk_gb: 150,
};

const cloudOptions = [
  { value: 'SECURE', label: 'Secure Cloud' },
  { value: 'COMMUNITY', label: 'Community Cloud' },
];

type Props = {
  jobConfig: JobConfig;
  setJobConfig: (value: any, key: string) => void;
  gpuIDs: string | null;
  setGpuIDs: (value: string | null) => void;
};

export default function RunpodSection({ jobConfig, setJobConfig, gpuIDs, setGpuIDs }: Props) {
  const process0: any = jobConfig.config.process[0];
  const runpod = process0.runpod ?? DEFAULT_RUNPOD;
  const enabled: boolean = !!runpod.enabled;

  const [gpus, setGpus] = useState<RunpodGpu[]>([]);
  const [gpuSource, setGpuSource] = useState<string>('');
  const [hasApiKey, setHasApiKey] = useState<boolean>(false);
  const [sshKeyPath, setSshKeyPath] = useState<string>('~/.ssh/id_ed25519');
  const [apiKeyInput, setApiKeyInput] = useState<string>('');
  const [credStatus, setCredStatus] = useState<'idle' | 'saving' | 'saved'>('idle');
  // remembers which local GPU was selected before flipping to remote
  const prevLocalGpu = useRef<string | null>(gpuIDs && gpuIDs !== 'runpod' ? gpuIDs : null);

  // Ensure the config carries a runpod block (older/imported configs may not).
  useEffect(() => {
    if (!process0.runpod) {
      setJobConfig({ ...DEFAULT_RUNPOD }, 'config.process[0].runpod');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fetchGpus = () => {
    apiClient
      .get('/api/runpod/gpus')
      .then(r => {
        setGpus(r.data.gpus || []);
        setGpuSource(r.data.source || '');
      })
      .catch(() => undefined);
  };

  useEffect(() => {
    fetchGpus();
    apiClient
      .get('/api/runpod/config')
      .then(r => {
        setHasApiKey(!!r.data.hasApiKey);
        if (r.data.RUNPOD_SSH_KEY_PATH) setSshKeyPath(r.data.RUNPOD_SSH_KEY_PATH);
      })
      .catch(() => undefined);
  }, []);

  const setEnabled = (value: boolean) => {
    setJobConfig(value, 'config.process[0].runpod.enabled');
    if (value) {
      if (gpuIDs && gpuIDs !== 'runpod') prevLocalGpu.current = gpuIDs;
      // RunPod jobs share one serialized queue lane under this sentinel.
      setGpuIDs('runpod');
    } else {
      setGpuIDs(prevLocalGpu.current ?? '0');
    }
  };

  const saveCreds = async () => {
    setCredStatus('saving');
    try {
      await apiClient.post('/api/runpod/config', {
        RUNPOD_API_KEY: apiKeyInput,
        RUNPOD_SSH_KEY_PATH: sshKeyPath,
      });
      if (apiKeyInput) setHasApiKey(true);
      setApiKeyInput('');
      setCredStatus('saved');
      // a freshly saved key unlocks the live GPU list
      fetchGpus();
      setTimeout(() => setCredStatus('idle'), 2000);
    } catch {
      setCredStatus('idle');
    }
  };

  const gpuOptions = gpus.map(g => ({ value: g.id, label: g.label }));
  // Make sure whatever is stored stays selectable even if not in the list.
  if (runpod.gpu_type && !gpuOptions.find(o => o.value === runpod.gpu_type)) {
    gpuOptions.unshift({ value: runpod.gpu_type, label: runpod.gpu_type });
  }

  return (
    <Card title="Remote Training (RunPod)">
      <Checkbox
        label="Train remotely on RunPod"
        checked={enabled}
        onChange={setEnabled}
      />
      <p className="text-xs text-gray-500 mt-1">
        Spins up a RunPod pod, runs this exact job there, streams logs &amp; checkpoints back, then terminates the pod.
      </p>

      {enabled && (
        <div className="mt-4 space-y-4">
          <SelectInput
            label="RunPod GPU"
            value={runpod.gpu_type}
            onChange={value => setJobConfig(value, 'config.process[0].runpod.gpu_type')}
            options={gpuOptions}
          />
          {gpuSource === 'fallback' && (
            <p className="text-xs text-amber-600 -mt-2">
              Showing a built-in GPU list. Add a RunPod API key below to load live availability.
            </p>
          )}
          <SelectInput
            label="Cloud Type"
            value={runpod.cloud_type}
            onChange={value => setJobConfig(value, 'config.process[0].runpod.cloud_type')}
            options={cloudOptions}
          />
          <NumberInput
            label="Container Disk (GB)"
            value={runpod.container_disk_gb}
            min={20}
            onChange={value => setJobConfig(value ?? 150, 'config.process[0].runpod.container_disk_gb')}
          />

          <div className="border-t border-gray-700 pt-4 space-y-3">
            <div className="text-sm font-medium text-gray-300">
              RunPod Credentials {hasApiKey && <span className="text-green-500">· API key saved ✓</span>}
            </div>
            <TextInput
              label="RunPod API Key"
              type="password"
              value={apiKeyInput}
              onChange={setApiKeyInput}
              placeholder={hasApiKey ? '•••••••• (leave blank to keep current)' : 'Enter your RunPod API key'}
            />
            <TextInput
              label="SSH Private Key Path"
              value={sshKeyPath}
              onChange={setSshKeyPath}
              placeholder="~/.ssh/id_ed25519"
            />
            <p className="text-xs text-gray-500">
              The matching public key must be added to your RunPod account. Get an API key at{' '}
              <a
                href="https://www.runpod.io/console/user/settings"
                target="_blank"
                rel="noreferrer"
                className="underline"
              >
                runpod.io console
              </a>
              .
            </p>
            <button
              type="button"
              onClick={saveCreds}
              disabled={credStatus === 'saving'}
              className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded-md text-sm disabled:opacity-50"
            >
              {credStatus === 'saving' ? 'Saving...' : credStatus === 'saved' ? 'Saved ✓' : 'Save Credentials'}
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}
