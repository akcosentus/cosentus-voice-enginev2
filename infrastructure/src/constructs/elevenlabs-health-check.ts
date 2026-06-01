import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwactions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';
import { VoiceEngineConfig, resourcePrefix } from '../config';

export interface ElevenLabsHealthCheckConstructProps {
  readonly config: VoiceEngineConfig;
  /** ARN of the Secrets Manager secret holding the ElevenLabs API key. */
  readonly elevenLabsSecretArn: string;
  /** SNS topic alarms publish to (shared with the MonitoringConstruct). */
  readonly alarmTopic: sns.ITopic;
  /**
   * How often to poll ElevenLabs. Defaults to every 15 minutes — the
   * subscription endpoint is free to call and the failure we care
   * about (a failed/past-due payment silently blocking TTS) does not
   * need sub-15-minute detection.
   */
  readonly schedule?: events.Schedule;
  /**
   * Alarm when remaining characters drop below this. Default 15000 —
   * roughly one Wave-7-style concurrent test run of headroom, so the
   * on-call gets warning before a real call hits a quota wall.
   */
  readonly charsRemainingThreshold?: number;
}

/**
 * ElevenLabs subscription health check — added 2026-06-01 after a
 * past-due ElevenLabs payment silently blocked TTS generation and
 * produced totally silent calls with NO error in the engine logs.
 * That cost a multi-call investigation; this turns the same failure
 * into an immediate CloudWatch alarm.
 *
 * What it does
 * ------------
 * An EventBridge schedule (default every 15 min) invokes a tiny
 * stdlib-only Python Lambda that:
 *   1. Reads the ElevenLabs API key from Secrets Manager.
 *   2. Calls GET https://api.elevenlabs.io/v1/user/subscription.
 *   3. Emits two CloudWatch metrics under VoiceAgent/Pipeline with
 *      an Environment dimension (matching the engine's metric
 *      contract):
 *        - ElevenLabsSubscriptionActive: 1 when status == "active",
 *          else 0. (past_due / canceled / unpaid all → 0.)
 *        - ElevenLabsCharsRemaining: character_limit - character_count.
 *
 * Two alarms publish to the shared SNS topic:
 *   - ElevenLabsSubscriptionInactive — fires when Active < 1. This is
 *     the exact signal that would have caught the 2026-05-28 outage
 *     (status was "past_due").
 *   - ElevenLabsCharsLow — fires when remaining characters fall below
 *     charsRemainingThreshold, so quota exhaustion (also a silent-TTS
 *     failure mode) pages before it bites a real call.
 *
 * Why a scheduled Lambda and not an engine-side check
 * ---------------------------------------------------
 * Same separation rationale as the task recycler: keep billing/vendor
 * health out of the call-path code. Runs once per tick regardless of
 * task count; no per-task duplication; no added latency on calls.
 *
 * Cost: one free ElevenLabs API call + 2 CloudWatch datapoints every
 * 15 min. Negligible. Does NOT consume TTS character quota (the
 * subscription endpoint is metadata-only).
 */
export class ElevenLabsHealthCheckConstruct extends Construct {
  public readonly function: lambda.Function;
  public readonly rule: events.Rule;
  public readonly logGroup: logs.LogGroup;
  public readonly subscriptionInactiveAlarm: cloudwatch.Alarm;
  public readonly charsLowAlarm: cloudwatch.Alarm;

  constructor(scope: Construct, id: string, props: ElevenLabsHealthCheckConstructProps) {
    super(scope, id);
    const {
      config,
      elevenLabsSecretArn,
      alarmTopic,
      schedule,
      charsRemainingThreshold = 15000,
    } = props;
    const prefix = resourcePrefix(config);
    const functionName = `${prefix}-elevenlabs-health`;

    this.logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const role = new iam.Role(this, 'ExecutionRole', {
      roleName: `${functionName}-role`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      // IAM role description is ASCII-only (em-dashes get rejected).
      description: 'ElevenLabs subscription health check - reads the EL key, polls subscription status, emits CloudWatch metrics.',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadElevenLabsKey',
        actions: ['secretsmanager:GetSecretValue'],
        resources: [elevenLabsSecretArn],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PutHealthMetrics',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'cloudwatch:namespace': 'VoiceAgent/Pipeline' },
        },
      }),
    );

    this.function = new lambda.Function(this, 'Function', {
      functionName,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: 'index.handler',
      memorySize: 128,
      timeout: cdk.Duration.seconds(20),
      role,
      logGroup: this.logGroup,
      description: (
        `ElevenLabs subscription health check (2026-06-01). Polls ` +
        `subscription status + remaining quota, emits VoiceAgent/Pipeline ` +
        `metrics. Catches the silent-TTS failure mode (past-due payment / ` +
        `quota exhaustion).`
      ),
      environment: {
        ELEVENLABS_SECRET_ARN: elevenLabsSecretArn,
        ENVIRONMENT: config.environment,
        METRIC_NAMESPACE: 'VoiceAgent/Pipeline',
      },
      code: lambda.Code.fromInline(`
"""ElevenLabs subscription health check.

Polls the ElevenLabs subscription endpoint and emits two CloudWatch
metrics so a past-due payment or quota exhaustion (both of which
silently block TTS generation) page the on-call instead of producing
silent calls. Stdlib-only (urllib) + boto3, which the Lambda runtime
provides.
"""
import json
import os
import urllib.request
import urllib.error

import boto3

_SECRET_ARN = os.environ["ELEVENLABS_SECRET_ARN"]
_ENV = os.environ.get("ENVIRONMENT", "unknown")
_NS = os.environ.get("METRIC_NAMESPACE", "VoiceAgent/Pipeline")
_SUB_URL = "https://api.elevenlabs.io/v1/user/subscription"

_sm = boto3.client("secretsmanager")
_cw = boto3.client("cloudwatch")


def _emit(active, chars_remaining):
    data = [{
        "MetricName": "ElevenLabsSubscriptionActive",
        "Dimensions": [{"Name": "Environment", "Value": _ENV}],
        "Value": float(active),
        "Unit": "None",
    }]
    if chars_remaining is not None:
        data.append({
            "MetricName": "ElevenLabsCharsRemaining",
            "Dimensions": [{"Name": "Environment", "Value": _ENV}],
            "Value": float(chars_remaining),
            "Unit": "Count",
        })
    _cw.put_metric_data(Namespace=_NS, MetricData=data)


def handler(event, _context):
    """EventBridge entry point. Emits Active (1/0) + CharsRemaining."""
    key = _sm.get_secret_value(SecretId=_SECRET_ARN)["SecretString"].strip()

    req = urllib.request.Request(_SUB_URL, headers={"xi-api-key": key})
    status = None
    chars_remaining = None
    active = 0
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        status = body.get("status")
        active = 1 if status == "active" else 0
        limit = body.get("character_limit")
        used = body.get("character_count")
        if isinstance(limit, int) and isinstance(used, int):
            chars_remaining = max(limit - used, 0)
    except urllib.error.HTTPError as exc:
        # A 401 here usually means payment_required / invalid key —
        # treat as inactive so the alarm fires.
        status = f"http_{exc.code}"
        active = 0
    except Exception as exc:  # noqa: BLE001 — emit inactive on any failure
        status = f"error:{type(exc).__name__}"
        active = 0

    _emit(active, chars_remaining)

    print(json.dumps({
        "op": "ElevenLabsHealthCheck",
        "environment": _ENV,
        "subscription_status": status,
        "active": active,
        "chars_remaining": chars_remaining,
    }))
    return {"active": active, "status": status, "chars_remaining": chars_remaining}
      `.trim()),
    });

    this.rule = new events.Rule(this, 'Schedule', {
      ruleName: `${prefix}-elevenlabs-health-schedule`,
      description: 'Polls ElevenLabs subscription status for the silent-TTS alarm (2026-06-01).',
      schedule: schedule ?? events.Schedule.rate(cdk.Duration.minutes(15)),
    });
    this.rule.addTarget(new targets.LambdaFunction(this.function));

    const alarmAction = new cwactions.SnsAction(alarmTopic);
    const envDim = { Environment: config.environment };

    this.subscriptionInactiveAlarm = new cloudwatch.Alarm(this, 'SubscriptionInactiveAlarm', {
      alarmName: `${prefix}-elevenlabs-subscription-inactive`,
      alarmDescription:
        'ElevenLabs subscription is NOT active (past_due / canceled / unpaid / key-rejected). ' +
        'TTS generation is blocked → calls go silent. Fix billing in the ElevenLabs dashboard. ' +
        'Added after the 2026-05-28 silent-call outage.',
      metric: new cloudwatch.Metric({
        namespace: 'VoiceAgent/Pipeline',
        metricName: 'ElevenLabsSubscriptionActive',
        dimensionsMap: envDim,
        statistic: 'Minimum',
        period: cdk.Duration.minutes(15),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      // NOT_BREACHING: before the first check runs (e.g. fresh deploy)
      // "no data" must not page. Once the schedule fires the metric is
      // present every 15 min.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    this.subscriptionInactiveAlarm.addAlarmAction(alarmAction);

    this.charsLowAlarm = new cloudwatch.Alarm(this, 'CharsLowAlarm', {
      alarmName: `${prefix}-elevenlabs-chars-low`,
      alarmDescription:
        `ElevenLabs remaining character quota < ${charsRemainingThreshold}. ` +
        'Quota exhaustion also blocks TTS (silent calls). Top up the plan.',
      metric: new cloudwatch.Metric({
        namespace: 'VoiceAgent/Pipeline',
        metricName: 'ElevenLabsCharsRemaining',
        dimensionsMap: envDim,
        statistic: 'Minimum',
        period: cdk.Duration.minutes(15),
      }),
      threshold: charsRemainingThreshold,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    this.charsLowAlarm.addAlarmAction(alarmAction);

    new cdk.CfnOutput(this, 'FunctionName', { value: this.function.functionName });
  }
}
