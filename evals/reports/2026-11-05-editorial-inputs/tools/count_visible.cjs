// Local WASM tokenizer only. TIKTOKEN_MODULE points to an already installed @dqbd/tiktoken.
const fs = require('node:fs');
const modulePath = process.env.TIKTOKEN_MODULE;
if (!modulePath || process.argv.length < 3) {
  throw new Error('Set TIKTOKEN_MODULE to a local @dqbd/tiktoken package and pass visible JSON files.');
}
const encoding = require(modulePath).get_encoding('o200k_base');
try {
  for (const file of process.argv.slice(2)) {
    const value = JSON.parse(fs.readFileSync(file, 'utf8'));
    const components = {
      instructions: encoding.encode(value.instructions).length,
      input: value.input.flatMap(message => message.content)
        .reduce((sum, content) => sum + encoding.encode(content.text).length, 0),
      schema: encoding.encode(JSON.stringify(value.schema)).length,
    };
    console.log(JSON.stringify({file, encoding: 'o200k_base', ...components,
      total: Object.values(components).reduce((sum, count) => sum + count, 0),
      provider_framing: false, endpoint_called: false}));
  }
} finally {
  encoding.free();
}
