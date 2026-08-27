export class NonRetryableBridgeExecutionError extends Error {
  reasonCode: string;

  constructor(reasonCode: string, message: string) {
    super(message);
    this.name = 'NonRetryableBridgeExecutionError';
    this.reasonCode = reasonCode;
  }
}
