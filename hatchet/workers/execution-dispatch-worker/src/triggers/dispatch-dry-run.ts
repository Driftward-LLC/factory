import { createDraftPrDispatch } from '../workflows/create-draft-pr-dispatch';

async function main() {
  const requestId = `dry-run-${Date.now()}`;
  const result = await createDraftPrDispatch.run({
    requestId,
    workItemId: 'ENG-35',
    repo: 'Driftward-LLC/factory',
    linearIssue: 'ENG-35',
    dryRun: true
  });

  console.log(JSON.stringify(result['submit-create-draft-pr'], null, 2));
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
