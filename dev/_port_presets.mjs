// 一次性移植工具：把老项目的 presets.mjs **原样**导出成 JSON。
//
// 为什么不手抄：那 6 段人格提示词是产品的灵魂，人手敲一遍必然出错，
// 而且错的地方（少个星号、多空一行）肉眼看不出来。
// 用 Node 直接 import 老模块再序列化，保真度由构造保证。
//
// 跑法：node dev/_port_presets.mjs   （跑完这份文件就可以删了，验收脚本会盯着结果）

import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const OLD = process.env.RT_OLD_SERVER || 'file:///D:/AAsanboxForDsh/server/presets.mjs';
const here = path.dirname(fileURLToPath(import.meta.url));

const old = await import(OLD);

const payload = {
  _source: OLD,
  providerPresets: old.PROVIDER_PRESETS,
  seedTemplates: old.SEED_HUMANS,
  bareTemplate: old.BARE_HUMAN,
  defaultSettings: old.DEFAULT_SETTINGS,
  defaultContextWindow: old.DEFAULT_CONTEXT_WINDOW,
  distillSystem: old.DISTILL_SYSTEM,
};

const out = path.join(here, '..', 'server', 'presets.json');
writeFileSync(out, JSON.stringify(payload, null, 2), 'utf8');

const n = payload.seedTemplates.length;
const chars = payload.seedTemplates.reduce((a, t) => a + t.system.length, 0);
console.log(`已导出 ${out}`);
console.log(`  平台模板 ${payload.providerPresets.length} 个`);
console.log(`  内置人格 ${n} 段，提示词合计 ${chars} 字`);
console.log(`  基线模板：${payload.bareTemplate.name}`);
console.log(`  提炼元提示词 ${payload.distillSystem.length} 字`);
