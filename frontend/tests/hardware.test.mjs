import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await fs.readFile(new URL('../src/lib/hardware.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const { hardwareSummary } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)

const capableGpu = {
  gpu: { name: 'Intel Iris Xe' },
  decode: { hardwareDecode: true, vaapiAvailable: true },
  inference: { preferredDevice: 'GPU', backendName: 'OpenVINO' },
  notes: [],
}

test('detected GPU and VAAPI capability do not override a CPU fallback or software policy', () => {
  const actual = hardwareSummary({
    ...capableGpu,
    inference: { ...capableGpu.inference, backend: { available: true, engine: 'OpenVINO', device: 'CPU', accelerated: false } },
    policy: { decode: 'software', decodeReason: 'hardware acceleration is switched off in Settings', gpuInferenceDisabled: 'CL_OUT_OF_RESOURCES' },
  })
  assert.equal(actual.inference, 'OpenVINO · CPU')
  assert.equal(actual.decode, 'Software')
  assert.equal(actual.decodeReason, 'hardware acceleration is switched off in Settings')
  assert.deepEqual(actual.notes, ['GPU inference is disabled; using CPU. See Advanced diagnostics for details.'])
})

test('unknown runtime stays unknown even if the detected device supports acceleration', () => {
  const actual = hardwareSummary(capableGpu)
  assert.equal(actual.inference, 'Device not reported')
  assert.equal(actual.decode, 'Not reported')
})

test('available acceleration and unavailable inference remain distinct', () => {
  const actual = hardwareSummary({
    ...capableGpu,
    inference: { backend: { available: false, engine: null, device: null } },
    policy: { decode: 'vaapi', decodeReason: null, gpuInferenceDisabled: null },
  })
  assert.equal(actual.inference, 'Unavailable')
  assert.equal(actual.decode, 'VAAPI')
  assert.deepEqual(actual.notes, ['No inference runtime is available.'])
})
