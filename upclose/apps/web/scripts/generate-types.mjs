import { mkdir, readFile, writeFile } from 'node:fs/promises';
import openapiTS, { astToString } from 'openapi-typescript';

// Resolve against this script, so root-level npm --prefix and direct use agree.
const schemaUrl = new URL('../../../services/api/schema/openapi.json', import.meta.url);
const outputUrl = new URL('../src/generated/contracts.ts', import.meta.url);
const check = process.argv.includes('--check');

try {
  const schema = JSON.parse(await readFile(schemaUrl, 'utf8'));
  const ast = await openapiTS(schema, { alphabetize: true });
  const output = '// Generated from services/api/schema/openapi.json. Do not edit.\n'
    + '// Regenerate the Pydantic schema first, then run npm --prefix apps/web run generate:types.\n'
    + astToString(ast).replaceAll('\r\n', '\n');

  if (check) {
    if (await readFile(outputUrl, 'utf8') !== output) {
      throw new Error('Generated contracts are stale. Run npm --prefix apps/web run generate:types.');
    }
    console.log('Generated contracts match the exported Pydantic schema.');
  } else {
    await mkdir(new URL('../src/generated/', import.meta.url), { recursive: true });
    await writeFile(outputUrl, output, 'utf8');
    console.log('Generated src/generated/contracts.ts from exported Pydantic OpenAPI.');
  }
} catch (error) {
  console.error('Type generation failed:', error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
