import { createDraftPrDispatch } from '../workflows/create-draft-pr-dispatch';

async function main() {
  const repo = process.env.PR_REPO;
  const linearIssue = process.env.LINEAR_ISSUE;
  if (!repo || !linearIssue) throw new Error('PR_REPO and LINEAR_ISSUE are required');
  const requestId = `dry-run-${Date.now()}`;
  const result = await createDraftPrDispatch.run({
    requestId,
    workItemId: linearIssue,
    repo,
    linearIssue,
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
