import prisma from '../prisma';
import { Job } from '@prisma/client';
import { spawn } from 'child_process';
import path from 'path';
import fs from 'fs';
import { TOOLKIT_ROOT, getTrainingFolder, getHFToken } from '../paths';
import { resolvePythonPath } from '../pythonPath';

// RunPod extension — spawns the Python orchestrator (ui_scripts/runpod_train.py)
// instead of a local `run.py`, when a job has runpod.enabled in its config.
// New file → survives upstream pulls. The only tracked-file edit is a 3-line
// hook at the top of cron/actions/startJob.ts that calls maybeStartRunpodJob().

const getSetting = async (key: string, fallback = ''): Promise<string> => {
  const row = await prisma.settings.findFirst({ where: { key } });
  return row?.value && row.value !== '' ? row.value : fallback;
};

const rotateLog = (logPath: string, trainingFolder: string) => {
  try {
    if (fs.existsSync(logPath)) {
      const logsFolder = path.join(trainingFolder, 'logs');
      if (!fs.existsSync(logsFolder)) fs.mkdirSync(logsFolder, { recursive: true });
      let num = 0;
      while (fs.existsSync(path.join(logsFolder, `${num}_log.txt`))) num++;
      fs.renameSync(logPath, path.join(logsFolder, `${num}_log.txt`));
    }
  } catch (e) {
    console.error('Error rotating log file:', e);
  }
};

/**
 * Returns true if the job is a RunPod job and was dispatched to the remote
 * orchestrator (caller should then return early). Returns false for normal
 * local jobs so the existing local-training path runs unchanged.
 */
export default async function maybeStartRunpodJob(job: Job): Promise<boolean> {
  let jobConfig: any;
  try {
    jobConfig = JSON.parse(job.job_config);
  } catch {
    return false;
  }
  const runpod = jobConfig?.config?.process?.[0]?.runpod;
  if (!runpod?.enabled) return false;

  const jobID = job.id;
  const trainingRoot = await getTrainingFolder();
  const trainingFolder = path.join(trainingRoot, job.name);
  if (!fs.existsSync(trainingFolder)) fs.mkdirSync(trainingFolder, { recursive: true });

  const configPath = path.join(trainingFolder, '.job_config.json');
  const logPath = path.join(trainingFolder, 'log.txt');
  rotateLog(logPath, trainingFolder);

  // Write the job config as-is; the orchestrator rewrites paths for the pod.
  fs.writeFileSync(configPath, JSON.stringify(jobConfig, null, 2));

  const apiKey = await getSetting('RUNPOD_API_KEY');
  const sshKeyPath = await getSetting('RUNPOD_SSH_KEY_PATH', '~/.ssh/id_ed25519');
  const hfToken = await getHFToken();

  if (!apiKey) {
    await prisma.job.update({
      where: { id: jobID },
      data: { status: 'error', info: 'RunPod API key not set (open the job form → Remote Training to add it)' },
    });
    return true;
  }

  const pythonPath = resolvePythonPath();
  const orchestrator = path.join(TOOLKIT_ROOT, 'ui_scripts', 'runpod_train.py');
  if (!fs.existsSync(orchestrator)) {
    await prisma.job.update({
      where: { id: jobID },
      data: { status: 'error', info: `RunPod orchestrator not found at ${orchestrator}` },
    });
    return true;
  }

  const args = [
    orchestrator,
    '--config', configPath,
    '--log', logPath,
    '--job-id', jobID,
    '--db', path.join(TOOLKIT_ROOT, 'aitk_db.db'),
    '--name', job.name,
    '--training-folder', trainingRoot,
  ];

  const additionalEnv: Record<string, string> = {
    AITK_JOB_ID: jobID,
    RUNPOD_API_KEY: apiKey,
    RUNPOD_SSH_KEY_PATH: sshKeyPath,
    PYTHONUNBUFFERED: '1',
  };
  if (hfToken && hfToken.trim() !== '') additionalEnv.HF_TOKEN = hfToken;

  try {
    const subprocess = spawn(pythonPath, args, {
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
      env: { ...process.env, ...additionalEnv },
      cwd: TOOLKIT_ROOT,
    });

    const pid = subprocess.pid ?? null;
    if (pid != null) {
      await prisma.job.update({ where: { id: jobID }, data: { pid } });
      try {
        fs.writeFileSync(path.join(trainingFolder, 'pid.txt'), String(pid), { flag: 'w' });
      } catch (e) {
        console.error('Error writing pid file:', e);
      }
    }
    subprocess.unref();
    console.log(`Started RunPod job ${jobID} (orchestrator pid ${pid})`);
  } catch (error: any) {
    await prisma.job.update({
      where: { id: jobID },
      data: { status: 'error', info: `Error launching RunPod job: ${error?.message || 'Unknown error'}` },
    });
  }
  return true;
}
