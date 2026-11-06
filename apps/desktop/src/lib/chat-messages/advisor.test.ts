import { expect, it } from 'vitest'

import { toChatMessages } from './index'

const BLOCK = [
  '[advisor — advisory only]',
  '- [concern · tool_choice · Architecture] re-read the failing test — decide: fix it',
  '- [nit · general] tidy the helper',
  '[end advisor]'
].join('\n')

it('hydrates an advisor row as a system card, not a user prompt', () => {
  const [message] = toChatMessages([{ role: 'user', content: BLOCK, display_kind: 'advisor', timestamp: 1 }])

  expect(message.role).toBe('system')
  expect(message.advisor?.severity).toBe('concern')
  expect(message.advisor?.label).toBe('concern · tool_choice · Architecture')
  expect(message.advisor?.body).toContain('re-read the failing test')
  expect(message.advisor?.body).not.toContain('[advisor')
  expect(message.advisor?.body).not.toContain('[end advisor]')
  expect(message.advisor?.items).toHaveLength(2)
})

it('lets a blocker listed second raise the card severity', () => {
  const block = [
    '[advisor — advisory only]',
    '- [concern · tool_choice · Architecture] check the import',
    '- [blocker · ai_self_representation_check · default] the test never ran',
    '[end advisor]'
  ].join('\n')
  const [message] = toChatMessages([{ role: 'user', content: block, display_kind: 'advisor', timestamp: 4 }])

  expect(message.advisor?.severity).toBe('blocker')
  expect(message.advisor?.label).toBe('blocker · ai_self_representation_check · default')
  expect(message.advisor?.items.map(item => item.severity)).toEqual(['concern', 'blocker'])
})

it('keeps an unparseable advisor row visible instead of dropping it', () => {
  const [message] = toChatMessages([
    { role: 'user', content: 'plain advice from an older host', display_kind: 'advisor', timestamp: 2 }
  ])

  expect(message.role).toBe('system')
  expect(message.advisor?.severity).toBe('advisory')
  expect(message.advisor?.body).toBe('plain advice from an older host')
})

it('leaves a normal user row alone', () => {
  const [message] = toChatMessages([{ role: 'user', content: 'real prompt', timestamp: 3 }])

  expect(message.role).toBe('user')
  expect(message.advisor).toBeUndefined()
})
