const fs = require('fs');
const f = 'public/admin.html';
let s = fs.readFileSync(f, 'utf8');
const rules = [
  ['데모 데이터', '샘플 데이터'],
  ['내려받습니다', '다운로드합니다'],
  ['내려받기', '다운로드'],
  ['상담원', '상담사'],
  ['통화 기록', '통화 이력'],
  ['통화로그', '통화 이력'],
  ['대시보드', '운영 현황'],
  ['한눈에', ''],
  [' · ', ', '],
];
const before = s.length;
for (const [a, b] of rules) s = s.split(a).join(b);
s = s.replace(/[\u{1F300}-\u{1FAFF}\u{FE0F}☀☎⚠✅⬇↺]\s?/gu, '');
fs.writeFileSync(f, s, 'utf8');
console.log('done', before, s.length);
