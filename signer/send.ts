/**
 * Signs and broadcasts a Getgems public-api transaction list with the bot wallet (v5r1).
 *
 * Input (stdin or --file): the `response` object returned by Getgems trading endpoints:
 *   { uuid, from, timeout, list: [{ to, amount, payload?, stateInit?, check, context }] }
 * Modes:
 *   --dry-run   build and print the messages, do not broadcast (default)
 *   --send      broadcast
 *   --balance   print wallet address and balance and exit
 *
 * Mnemonic is read from macOS keychain item `getgems-flipper-wallet` (account `kirillll`);
 * override with env MNEMONIC for tests. Never logs the key.
 */
import { execSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { Address, beginCell, Cell, internal, SendMode, toNano } from '@ton/core';
import { mnemonicToPrivateKey } from '@ton/crypto';
import { TonClient, WalletContractV5R1 } from '@ton/ton';

const args = process.argv.slice(2);
const has = (f: string) => args.includes(f);
const arg = (f: string) => { const i = args.indexOf(f); return i >= 0 ? args[i + 1] : undefined; };

const MAX_SINGLE_TON = Number(process.env.MAX_SINGLE_TON ?? '300');   // hard cap per broadcast, safety net

/** Keyless toncenter allows ~1 req/s and answers 429 above that: retry with backoff. */
async function rpc<T>(fn: () => Promise<T>, tries = 6): Promise<T> {
  let last: unknown;
  for (let i = 0; i < tries; i++) {
    try { return await fn(); } catch (e) { last = e; await new Promise((r) => setTimeout(r, (process.env.TONCENTER_KEY ? 400 : 1500) * (i + 1))); }
  }
  throw last;
}

function mnemonic(): string[] {
  const env = process.env.MNEMONIC;
  const raw = env ?? execSync(
    'security find-generic-password -a kirillll -s getgems-flipper-wallet -w', { encoding: 'utf8' }).trim();
  const words = raw.split(/\s+/);
  if (words.length !== 24) throw new Error('mnemonic must be 24 words');
  return words;
}

async function main() {
  const endpoint = process.env.TON_ENDPOINT ?? 'https://toncenter.com/api/v2/jsonRPC';
  const client = new TonClient({ endpoint, apiKey: process.env.TONCENTER_KEY });
  if (!process.env.TONCENTER_KEY) console.error('note: no TONCENTER_KEY, using the keyless endpoint (rate limited)');
  const key = await mnemonicToPrivateKey(mnemonic());
  const wallet = WalletContractV5R1.create({ workchain: 0, publicKey: key.publicKey });
  const contract = client.open(wallet);

  if (has('--balance')) {
    const bal = await rpc(() => contract.getBalance());
    console.log(JSON.stringify({ address: wallet.address.toString({ bounceable: false }), balanceTon: Number(bal) / 1e9 }));
    return;
  }

  const file = arg('--file');
  const input = JSON.parse(file ? readFileSync(file, 'utf8') : readFileSync(0, 'utf8'));
  const tx = input.response ?? input;
  const list: Array<{ to: string; amount: string; payload?: string | null; stateInit?: string | null }> = tx.list;
  if (!Array.isArray(list) || list.length === 0) throw new Error('empty tx list');
  if (tx.from && !Address.parse(tx.from).equals(wallet.address)) {
    throw new Error(`tx.from ${tx.from} != bot wallet ${wallet.address.toString()}`);
  }
  const total = list.reduce((s, m) => s + Number(m.amount) / 1e9, 0);
  if (total > MAX_SINGLE_TON) throw new Error(`total ${total} TON exceeds MAX_SINGLE_TON=${MAX_SINGLE_TON}`);

  const messages = list.map((m) => internal({
    to: Address.parse(m.to),
    value: BigInt(m.amount),
    bounce: true,
    body: m.payload ? Cell.fromBase64(m.payload) : undefined,
    init: m.stateInit ? (() => { const c = Cell.fromBase64(m.stateInit!); const s = c.beginParse();
      return { code: s.loadRef(), data: s.loadRef() }; })() : undefined,
  }));
  // stateInit above assumes a plain (code, data) StateInit cell with two refs; Getgems offers use that shape.
  // If a stateInit carries split_depth/special flags, parse via loadStateInit instead.

  console.error(JSON.stringify({ wallet: wallet.address.toString({ bounceable: false }), messages: list.length,
    totalTon: total, timeout: tx.timeout, uuid: tx.uuid }));
  if (!has('--send')) { console.log(JSON.stringify({ dryRun: true, totalTon: total, messages: list.length })); return; }

  const seqno = await rpc(() => contract.getSeqno());
  await rpc(() => contract.sendTransfer({ seqno, secretKey: key.secretKey, sendMode: SendMode.PAY_GAS_SEPARATELY | SendMode.IGNORE_ERRORS, messages }), 3);
  // wait for seqno to advance
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 2500));
    const s = await rpc(() => contract.getSeqno(), 3).catch(() => seqno);
    if (s > seqno) { console.log(JSON.stringify({ sent: true, seqno: s, uuid: tx.uuid })); return; }
  }
  console.log(JSON.stringify({ sent: 'unknown', seqno, uuid: tx.uuid }));
  process.exitCode = 2;
}

main().catch((e) => { console.error(String(e?.message ?? e)); process.exit(1); });
