import type { RecapNotes } from '@/api/live'

/** Completely fictional mock notes. No recording or meeting content is used. */
export const MOCK_NOTES: RecapNotes = {
  headline: 'Sample library app tutorial is ready',
  summary: 'The fictional team discussed the imaginary search screen and a help menu.',
  keyPoints: ['The search demo works.', 'The sample documentation needs an index.'],
  decisions: ['Write an example tutorial.', 'Add a help menu to the demo.'],
  actionItems: [
    { owner: 'Alice Example', task: 'Draft a sample tutorial', due: 'Tuesday' },
    { owner: 'Bob Sample', task: 'Add a demo help menu', due: null },
  ],
  quotes: [{ speaker: 'Alice Example', quote: 'This fictional example is not a real meeting.' }],
  speakers: [
    { name: 'Alice Example', contribution: 'Led the demo.' },
    { name: 'Bob Sample', contribution: 'Showed the fake search screen.' },
  ],
  openQuestions: ['Should the example include an index page?'],
  topics: ['Tutorial', 'Demo'],
}
