import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwactions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';
import { VoiceEngineConfig, resourcePrefix } from '../config';

export interface MonitoringConstructProps {
  readonly config: VoiceEngineConfig;
  readonly cluster: ecs.ICluster;
  readonly service: ecs.IBaseService;
  readonly serviceName: string;
  // Concrete types so we can read `targetGroupFullName` and
  // `loadBalancerFullName` for CloudWatch alarm dimensions — those
  // attributes live on the concrete class, not the interface.
  readonly targetGroup: elbv2.ApplicationTargetGroup;
  readonly loadBalancer: elbv2.ApplicationLoadBalancer;
}

/**
 * CloudWatch alarms + dashboard for the voice engine.
 *
 * SNS topic for alarm fan-out
 * ---------------------------
 * One topic per env, named `${prefix}-alarms`. Subscriptions
 * (email / SMS / PagerDuty / Slack) are added manually via Console after
 * first deploy — we don't bake operator endpoints into IaC because that
 * pulls personal data into source control and creates merge friction
 * when on-call shifts.
 *
 * The five alarms (per the Layer 11 brief)
 * ----------------------------------------
 *   1. HighMemoryUtilization        — `AWS/ECS MemoryUtilization` Average
 *                                     > 80 for 5 consecutive 1-minute
 *                                     periods. Headroom check vs the
 *                                     2 GB task budget; Layer 9.5 peak
 *                                     was ~25%, so anything north of
 *                                     80% is a regression.
 *   2. HighCPUUtilization           — `AWS/ECS CPUUtilization` Average
 *                                     > 80 for 5×1 min. Layer 9.5 peak
 *                                     was 66%; alarm fires before
 *                                     latency degrades.
 *   3. TargetUnhealthy              — `AWS/ApplicationELB UnHealthyHostCount`
 *                                     Maximum >= 1 for 5×1 min. Catches
 *                                     /ready failing → ALB pulling tasks
 *                                     out of rotation.
 *   4. DrainTimeoutsAboveThreshold  — `Cosentus/VoiceEngine DrainTimeouts`
 *                                     Sum > 0 over 1 hour. Engine emits
 *                                     this when graceful shutdown exceeds
 *                                     its budget (wired in Wave 4).
 *   5. ActiveSessionsApproachingMax — `Cosentus/VoiceEngine ActiveSessions`
 *                                     fleet-Sum > 20 for 2×1 min. Early
 *                                     warning vs the prod max of
 *                                     6 × 25 = 150 concurrent.
 *
 * `treatMissingData: NOT_BREACHING` for all five — until Wave 4 ships
 * the custom-metric emitters, "no data" must not page the on-call.
 *
 * Dashboard
 * ---------
 * One dashboard per env: fleet active sessions, per-task average
 * sessions, RunningTaskCount, CPU/memory, ALB request count + target
 * response time, target healthy/unhealthy hosts. Cost-per-call is
 * deferred to Wave 5 (needs custom metric multi-source maths).
 */
export class MonitoringConstruct extends Construct {
  public readonly alarmTopic: sns.Topic;
  public readonly dashboard: cloudwatch.Dashboard;

  constructor(scope: Construct, id: string, props: MonitoringConstructProps) {
    super(scope, id);
    const {
      config,
      cluster,
      service,
      serviceName,
      targetGroup,
      loadBalancer,
    } = props;
    const prefix = resourcePrefix(config);

    this.alarmTopic = new sns.Topic(this, 'AlarmTopic', {
      topicName: `${prefix}-alarms`,
      displayName: `${prefix} CloudWatch alarms`,
    });

    const alarmAction = new cwactions.SnsAction(this.alarmTopic);

    const ecsDimensions = {
      ServiceName: serviceName,
      ClusterName: cluster.clusterName,
    };

    const albDimensions = {
      LoadBalancer: loadBalancer.loadBalancerFullName,
      TargetGroup: targetGroup.targetGroupFullName,
    };

    const envDim = { Environment: config.environment };

    const memoryAlarm = new cloudwatch.Alarm(this, 'HighMemoryAlarm', {
      alarmName: `${prefix}-memory-utilization-high`,
      alarmDescription: 'ECS MemoryUtilization > 80% sustained 5 min.',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/ECS',
        metricName: 'MemoryUtilization',
        dimensionsMap: ecsDimensions,
        statistic: 'Average',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 80,
      evaluationPeriods: 5,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    memoryAlarm.addAlarmAction(alarmAction);

    const cpuAlarm = new cloudwatch.Alarm(this, 'HighCpuAlarm', {
      alarmName: `${prefix}-cpu-utilization-high`,
      alarmDescription: 'ECS CPUUtilization > 80% sustained 5 min.',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/ECS',
        metricName: 'CPUUtilization',
        dimensionsMap: ecsDimensions,
        statistic: 'Average',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 80,
      evaluationPeriods: 5,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    cpuAlarm.addAlarmAction(alarmAction);

    const targetHealthAlarm = new cloudwatch.Alarm(this, 'TargetUnhealthyAlarm', {
      alarmName: `${prefix}-target-unhealthy`,
      alarmDescription: 'ALB target group has >= 1 unhealthy host for 5 min.',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/ApplicationELB',
        metricName: 'UnHealthyHostCount',
        dimensionsMap: albDimensions,
        statistic: 'Maximum',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 1,
      evaluationPeriods: 5,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    targetHealthAlarm.addAlarmAction(alarmAction);

    const drainTimeoutsAlarm = new cloudwatch.Alarm(this, 'DrainTimeoutsAlarm', {
      alarmName: `${prefix}-drain-timeouts`,
      alarmDescription:
        'Engine reported one or more graceful-drain timeouts in the last hour. ' +
        'Wave 4 wires the custom-metric emitter; before Wave 4 the alarm cannot fire.',
      metric: new cloudwatch.Metric({
        namespace: 'Cosentus/VoiceEngine',
        metricName: 'DrainTimeouts',
        dimensionsMap: envDim,
        statistic: 'Sum',
        period: cdk.Duration.hours(1),
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    drainTimeoutsAlarm.addAlarmAction(alarmAction);

    const activeSessionsAlarm = new cloudwatch.Alarm(this, 'ActiveSessionsHighAlarm', {
      alarmName: `${prefix}-active-sessions-high`,
      alarmDescription:
        'Fleet-wide ActiveSessions > 20 for 2 min — early warning vs the max ' +
        '(6 × maxCapacity).',
      metric: new cloudwatch.Metric({
        namespace: 'Cosentus/VoiceEngine',
        metricName: 'ActiveSessions',
        dimensionsMap: envDim,
        statistic: 'Sum',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 20,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    activeSessionsAlarm.addAlarmAction(alarmAction);

    this.dashboard = this.buildDashboard({
      prefix,
      envDim,
      ecsDimensions,
      albDimensions,
    });
    // Service + cluster references are kept on the public Construct
    // surface for downstream callers; the dashboard uses dimensions
    // directly.
    void service;
    void serviceName;
    void cluster;
  }

  private buildDashboard(args: {
    prefix: string;
    envDim: Record<string, string>;
    ecsDimensions: Record<string, string>;
    albDimensions: Record<string, string>;
  }): cloudwatch.Dashboard {
    const { prefix, envDim, ecsDimensions, albDimensions } = args;

    const dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `${prefix}-monitoring`,
    });

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Active sessions (fleet sum)',
        left: [
          new cloudwatch.Metric({
            namespace: 'Cosentus/VoiceEngine',
            metricName: 'ActiveSessions',
            dimensionsMap: envDim,
            statistic: 'Sum',
            period: cdk.Duration.minutes(1),
          }),
        ],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Sessions per task (average)',
        left: [
          new cloudwatch.Metric({
            namespace: 'Cosentus/VoiceEngine',
            metricName: 'ActiveSessions',
            dimensionsMap: envDim,
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
          }),
        ],
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'CPU utilization (%)',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/ECS',
            metricName: 'CPUUtilization',
            dimensionsMap: ecsDimensions,
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
          }),
        ],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Memory utilization (%)',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/ECS',
            metricName: 'MemoryUtilization',
            dimensionsMap: ecsDimensions,
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
          }),
        ],
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'ALB request count + target response time',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/ApplicationELB',
            metricName: 'RequestCount',
            dimensionsMap: albDimensions,
            statistic: 'Sum',
            period: cdk.Duration.minutes(1),
          }),
        ],
        right: [
          new cloudwatch.Metric({
            namespace: 'AWS/ApplicationELB',
            metricName: 'TargetResponseTime',
            dimensionsMap: albDimensions,
            statistic: 'p95',
            period: cdk.Duration.minutes(1),
          }),
        ],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Healthy / unhealthy targets',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/ApplicationELB',
            metricName: 'HealthyHostCount',
            dimensionsMap: albDimensions,
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
          }),
          new cloudwatch.Metric({
            namespace: 'AWS/ApplicationELB',
            metricName: 'UnHealthyHostCount',
            dimensionsMap: albDimensions,
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
          }),
        ],
        width: 12,
        height: 6,
      }),
    );

    return dashboard;
  }
}
