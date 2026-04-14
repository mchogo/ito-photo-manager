import { test, expect, type Page, type Route } from '@playwright/test';

type Role = 'admin' | 'worker';

type Slot = {
  slot_id: string;
  label: string;
  photo_filename: string | null;
  uploaded_at: string | null;
  retake_instruction: string | null;
  retake_requested_at: string | null;
};

type Project = {
  project_id: string;
  site_id: string;
  work_date: string;
  worker_name: string;
  created_at: string;
  equipment: Array<{ equipment_id: string; name: string; slots: Slot[] }>;
  project_name: string | null;
  project_number: string | null;
  address: string | null;
  status: string;
  memo: string | null;
  description: string | null;
  work_start_time: string | null;
  work_end_time: string | null;
  scheduled_date: string | null;
  survey_notes: string | null;
  documents: Array<{
    document_id: string;
    project_id: string;
    document_type: string;
    original_filename: string;
    stored_filename: string;
    size_bytes: number;
    uploaded_at: string;
    resubmit_instruction: string | null;
    resubmit_requested_at: string | null;
  }>;
  departure_time: string | null;
  arrival_time: string | null;
  checkout_time: string | null;
  approved_at: string | null;
};

const EQUIPMENT = [
  {
    equipment_id: 'pos_register',
    name: 'POSレジ',
    photo_slots: [{ slot_id: 'front', label: '正面' }],
  },
];

const MASTER_CONFIG = {
  statuses: [
    { value: '対応中', color: 'indigo' },
    { value: '案件終了', color: 'gray' },
  ],
  document_types: [
    { value: '依頼シート', category: '管理共有' },
    { value: '現地調査報告', category: '現地調査' },
    { value: '完成図書_設置', category: '設置' },
  ],
};

function jwtFor(role: Role, displayName: string): string {
  const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(
    JSON.stringify({ sub: `${role}-1`, role, display_name: displayName }),
  ).toString('base64url');
  return `${header}.${payload}.sig`;
}

async function setAuth(page: Page, role: Role): Promise<void> {
  const token = role === 'admin' ? jwtFor('admin', '管理者') : jwtFor('worker', '作業員');
  await page.addInitScript((t) => {
    window.localStorage.setItem('pm_authToken', t);
  }, token);
}

function makeProject(projectId: string): Project {
  return {
    project_id: projectId,
    site_id: 'SITE-001',
    work_date: '2026-04-14',
    worker_name: '田中太郎',
    created_at: '2026-04-14T00:00:00Z',
    equipment: [
      {
        equipment_id: 'pos_register',
        name: 'POSレジ',
        slots: [{ slot_id: 'front', label: '正面', photo_filename: null, uploaded_at: null, retake_instruction: null, retake_requested_at: null }],
      },
    ],
    project_name: '本番案件A',
    project_number: 'PJ-001',
    address: '東京都千代田区1-1',
    status: '対応中',
    memo: null,
    description: null,
    work_start_time: null,
    work_end_time: null,
    scheduled_date: '2026-04-14',
    survey_notes: null,
    documents: [],
    departure_time: null,
    arrival_time: null,
    checkout_time: null,
    approved_at: null,
  };
}

function validationOf(project: Project) {
  const slots = project.equipment.flatMap((eq) => eq.slots.map((slot) => ({ eq, slot })));
  const missing = slots
    .filter(({ slot }) => !slot.photo_filename)
    .map(({ eq, slot }) => ({
      equipment_id: eq.equipment_id,
      equipment_name: eq.name,
      slot_id: slot.slot_id,
      slot_label: slot.label,
    }));
  return {
    is_complete: missing.length === 0,
    missing_slots: missing,
    total_slots: slots.length,
    filled_slots: slots.length - missing.length,
  };
}

async function fulfillJson(route: Route, body: unknown): Promise<void> {
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

function getMultipartField(body: string | null, fieldName: string): string | null {
  if (!body) return null;
  const marker = `name="${fieldName}"`;
  const markerIndex = body.indexOf(marker);
  if (markerIndex < 0) return null;

  const valueStart = body.indexOf('\r\n\r\n', markerIndex);
  if (valueStart < 0) return null;

  const valueEnd = body.indexOf('\r\n', valueStart + 4);
  if (valueEnd < 0) return null;

  return body.slice(valueStart + 4, valueEnd);
}

test('案件作成フロー', async ({ page }) => {
  await setAuth(page, 'worker');
  const project = makeProject('p-create-1');

  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());

    if (url.pathname === '/api/equipment' && req.method() === 'GET') return fulfillJson(route, EQUIPMENT);
    if (url.pathname === '/api/master-config' && req.method() === 'GET') return fulfillJson(route, MASTER_CONFIG);
    if (url.pathname === '/api/projects' && req.method() === 'POST') return fulfillJson(route, project);
    if (url.pathname === `/api/projects/${project.project_id}` && req.method() === 'GET') return fulfillJson(route, project);
    if (url.pathname === `/api/projects/${project.project_id}/validate` && req.method() === 'GET') return fulfillJson(route, validationOf(project));
    if (url.pathname === '/api/projects' && req.method() === 'GET') return fulfillJson(route, [project]);

    return route.fulfill({ status: 404, body: 'not mocked' });
  });

  await page.goto('/');
  await page.getByPlaceholder('例: SITE-001').fill('SITE-001');
  await page.getByPlaceholder('例: 田中太郎').fill('田中太郎');
  await page.getByRole('checkbox').first().check();
  await page.getByRole('button', { name: '撮影開始' }).click();

  await expect(page).toHaveURL(/\/shoot\?projectId=p-create-1/);
  await expect(page.getByText('SITE-001')).toBeVisible();
  await expect(page.getByText('0/1')).toBeVisible();
});

test('写真アップロード〜完了判定', async ({ page }) => {
  await setAuth(page, 'worker');
  const project = makeProject('p-photo-1');

  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());

    if (url.pathname === '/api/master-config' && req.method() === 'GET') return fulfillJson(route, MASTER_CONFIG);
    if (url.pathname === `/api/projects/${project.project_id}` && req.method() === 'GET') return fulfillJson(route, project);
    if (url.pathname === `/api/projects/${project.project_id}/validate` && req.method() === 'GET') return fulfillJson(route, validationOf(project));
    if (url.pathname === `/api/projects/${project.project_id}/photos` && req.method() === 'POST') {
      project.equipment[0].slots[0].photo_filename = 'photo-front.jpg';
      project.equipment[0].slots[0].uploaded_at = '2026-04-14T10:00:00Z';
      return fulfillJson(route, {
        filename: 'photo-front.jpg',
        equipment_id: 'pos_register',
        slot_id: 'front',
        uploaded_at: '2026-04-14T10:00:00Z',
      });
    }
    if (url.pathname === '/api/projects' && req.method() === 'GET') return fulfillJson(route, [project]);

    return route.fulfill({ status: 404, body: 'not mocked' });
  });

  await page.goto(`/shoot?projectId=${project.project_id}`);
  await expect(page.getByText('残り 1 枚 — 全て撮影してください')).toBeVisible();

  const chooserPromise = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: '選択' }).first().click();
  const chooser = await chooserPromise;
  await chooser.setFiles({
    name: 'front.jpg',
    mimeType: 'image/jpeg',
    buffer: Buffer.from('fake image binary'),
  });

  await expect(page.getByRole('button', { name: 'プレビュー / 提出へ' })).toBeVisible();
  await page.getByRole('button', { name: 'プレビュー / 提出へ' }).click();

  await expect(page).toHaveURL(/\/preview\?projectId=p-photo-1/);
  await expect(page.getByText('1/1枚完了')).toBeVisible();
});

test('書類アップロード〜案件承認', async ({ page }) => {
  await setAuth(page, 'admin');
  const project = makeProject('p-doc-1');
  project.status = '図書提出待ち';

  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());

    if (url.pathname === '/api/master-config' && req.method() === 'GET') return fulfillJson(route, MASTER_CONFIG);
    if (url.pathname === `/api/projects/${project.project_id}` && req.method() === 'GET') return fulfillJson(route, project);
    if (url.pathname === `/api/projects/${project.project_id}/validate` && req.method() === 'GET') return fulfillJson(route, validationOf(project));

    if (url.pathname === `/api/projects/${project.project_id}/documents` && req.method() === 'POST') {
      const uploadedDocumentType = getMultipartField(req.postData(), 'document_type') ?? '不明';
      project.documents = [
        {
          document_id: 'doc-1',
          project_id: project.project_id,
          document_type: uploadedDocumentType,
          original_filename: 'completion-installation.pdf',
          stored_filename: 'stored-completion-installation.pdf',
          size_bytes: 1024,
          uploaded_at: '2026-04-14T11:00:00Z',
          resubmit_instruction: null,
          resubmit_requested_at: null,
        },
      ];
      if (project.status === '図書提出待ち' && ['完成図書_調査', '完成図書_設置'].includes(uploadedDocumentType)) {
        project.status = '成果物提出待ち';
      }
      return fulfillJson(route, project.documents[0]);
    }

    if (url.pathname === '/api/projects' && req.method() === 'GET') return fulfillJson(route, [project]);
    if (url.pathname === `/api/projects/${project.project_id}/approve` && req.method() === 'POST') {
      project.status = '案件終了';
      project.approved_at = '2026-04-14T12:00:00Z';
      return fulfillJson(route, project);
    }

    return route.fulfill({ status: 404, body: 'not mocked' });
  });

  await page.goto(`/projects/${project.project_id}`);
  await page.getByRole('button', { name: '書類管理' }).click();

  const installationSection = page.locator('div.liquid-glass').filter({ has: page.getByRole('heading', { name: '設置' }) }).first();
  const uploadInput = installationSection.locator('input[type="file"]');
  await uploadInput.setInputFiles({
    name: 'completion-installation.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from('dummy pdf'),
  });

  await expect(page.getByText('📄 completion-installation.pdf')).toBeVisible();

  await page.goto('/admin');
  await page.getByRole('button', { name: '✓ 承認して案件終了' }).first().click();
  await expect(page.getByText('案件終了').first()).toBeVisible();
});
