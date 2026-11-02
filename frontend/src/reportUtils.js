// Preserve legacy claims and punctuation. Never infer conclusions from old prose.
export function splitLegacyText(value) {
  const text=String(value||'').trim();
  if(!text)return [];
  return text.split(/\n+|(?<=[。！？；])\s*/u).map(p=>p.trim()).filter(Boolean);
}
