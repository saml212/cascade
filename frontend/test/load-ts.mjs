import { readFile } from 'node:fs/promises';

import ts from 'typescript';

const compilerOptions = {
  module: ts.ModuleKind.ESNext,
  target: ts.ScriptTarget.ES2022,
};

export function dataUrl(source) {
  return `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
}

export async function transpileTs(sourceUrl) {
  const source = await readFile(sourceUrl, 'utf8');
  return ts.transpileModule(source, { compilerOptions }).outputText;
}

export async function importTs(sourceUrl) {
  return import(dataUrl(await transpileTs(sourceUrl)));
}
