import * as cdk from 'aws-cdk-lib';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { VoiceEngineConfig, resourcePrefix } from '../config';

export interface TaskRecyclerConstructProps {
  readonly config: VoiceEngineConfig;
  /** ECS cluster ARN the recycler is scoped to. */
  readonly clusterArn: string;
  /** ECS cluster name (passed as env to the Lambda for the API call). */
  readonly clusterName: string;
  /** ECS service ARN — IAM is restricted to this exact ARN. */
  readonly serviceArn: string;
  /** ECS service name (passed as env to the Lambda for the API call). */
  readonly serviceName: string;
  /**
   * EventBridge schedule for recycle invocations. Defaults to once an
   * hour (``rate(1 hour)``), which keeps the per-task RSS under 55%
   * at the worst-case 0.45 %/min leak rate observed in Wave 6.
   */
  readonly schedule?: events.Schedule;
}

/**
 * Periodic task recycler — Wave 6 Phase B1.
 *
 * Forces a rolling deployment on the engine ECS service every
 * ``schedule`` interval (default 1 h). With the service's
 * ``minHealthyPercent=100`` setting, ECS spins up a new task before
 * tearing down the old one, so traffic is uninterrupted. After the
 * rollout, all running tasks are fresh and the per-task memory leak
 * (Pipecat #3750 / residual cycle leak) starts over from a clean
 * baseline.
 *
 * Why not engine-side self-recycle
 * --------------------------------
 * Considered. Adds operational complexity to the call-path code for
 * a problem that infrastructure can solve cleanly. Engine layer
 * stays focused on calls.
 *
 * Operational safety
 * ------------------
 * IAM is scoped to the SPECIFIC service ARN. The Lambda can call
 * ``ecs:UpdateService`` and ``ecs:DescribeServices`` on this one
 * service and nothing else.
 *
 * If a rolling deployment is already in flight when the schedule
 * fires (e.g., a regular CD push), the second
 * ``forceNewDeployment`` is a no-op — ECS treats consecutive
 * ``UpdateService`` calls during an active deploy as a single
 * deploy. Documented in the AWS ECS API reference.
 */
export class TaskRecyclerConstruct extends Construct {
  public readonly function: lambda.Function;
  public readonly rule: events.Rule;
  public readonly logGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: TaskRecyclerConstructProps) {
    super(scope, id);
    const { config, clusterArn, clusterName, serviceArn, serviceName, schedule } = props;
    const prefix = resourcePrefix(config);

    const functionName = `${prefix}-task-recycler`;

    this.logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const role = new iam.Role(this, 'ExecutionRole', {
      roleName: `${functionName}-role`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      // IAM role description is ASCII-only (same restriction as EC2
      // security group descriptions — em-dashes get rejected). Use
      // hyphen-minus.
      description: 'Task recycler - forces rolling deployment on the engine ECS service.',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EcsServiceRecycle',
        actions: ['ecs:UpdateService', 'ecs:DescribeServices'],
        resources: [serviceArn],
      }),
    );

    this.function = new lambda.Function(this, 'Function', {
      functionName,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: 'index.handler',
      memorySize: 128,
      timeout: cdk.Duration.seconds(30),
      role,
      logGroup: this.logGroup,
      description: (
        `Periodic ECS rolling-deploy trigger. Wave 6 Phase B1 (2026-05-18). ` +
        `Recycles ${serviceName} every schedule tick to bound the residual ` +
        `Pipecat memory leak.`
      ),
      environment: {
        ECS_CLUSTER: clusterName,
        ECS_SERVICE: serviceName,
        STUB_ENVIRONMENT: config.environment,
      },
      code: lambda.Code.fromInline(`
"""Task recycler — forces a rolling ECS deployment on schedule.

Bounds the residual memory leak from Pipecat 1.1.0 + daily-python /
grpcio native code. Each invocation triggers an ECS rolling
deployment; with minHealthyPercent=100 on the service, ECS spins up
a fresh task before tearing down the old one, so traffic continues
uninterrupted while leaked memory is reclaimed at the task boundary.

Idempotent. If a deploy is already in flight, AWS treats consecutive
UpdateService calls as a single deployment cycle.
"""
import json
import os
import time

import boto3

_CLUSTER = os.environ["ECS_CLUSTER"]
_SERVICE = os.environ["ECS_SERVICE"]
_ENV = os.environ.get("STUB_ENVIRONMENT", "unknown")


def handler(event, _context):
    """EventBridge schedule entry point. Returns {status, deployment_id}."""
    started = time.time()
    client = boto3.client("ecs")

    # Observe pre-state for the log line.
    pre = client.describe_services(cluster=_CLUSTER, services=[_SERVICE])["services"][0]
    pre_running = int(pre.get("runningCount", 0))
    pre_desired = int(pre.get("desiredCount", 0))

    # Trigger rolling deploy. forceNewDeployment uses the existing task
    # definition revision — no template change required.
    response = client.update_service(
        cluster=_CLUSTER,
        service=_SERVICE,
        forceNewDeployment=True,
    )
    deployment = response["service"]["deployments"][0]

    print(json.dumps({
        "op": "RecycleTask",
        "environment": _ENV,
        "cluster": _CLUSTER,
        "service": _SERVICE,
        "deployment_id": deployment.get("id"),
        "deployment_status": deployment.get("status"),
        "pre_running": pre_running,
        "pre_desired": pre_desired,
        "elapsed_ms": int((time.time() - started) * 1000),
    }))

    return {"status": "deploy_triggered", "deployment_id": deployment.get("id")}
      `.trim()),
    });

    this.rule = new events.Rule(this, 'Schedule', {
      ruleName: `${prefix}-task-recycler-schedule`,
      description: `Triggers task recycler every interval to bound memory leak (Wave 6 Phase B1).`,
      schedule: schedule ?? events.Schedule.rate(cdk.Duration.hours(1)),
    });
    this.rule.addTarget(new targets.LambdaFunction(this.function));

    // CloudFormation references that the user might want.
    void clusterArn;

    new cdk.CfnOutput(this, 'FunctionName', { value: this.function.functionName });
    new cdk.CfnOutput(this, 'ScheduleArn', { value: this.rule.ruleArn });
  }
}
