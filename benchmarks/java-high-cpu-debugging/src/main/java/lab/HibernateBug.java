package lab;

import org.hibernate.FlushMode;
import org.hibernate.Session;
import org.hibernate.SessionFactory;
import org.hibernate.Transaction;
import org.hibernate.boot.registry.StandardServiceRegistry;
import org.hibernate.boot.registry.StandardServiceRegistryBuilder;
import org.hibernate.cfg.Configuration;

import java.util.concurrent.ThreadLocalRandom;

/**
 * Bug 3: Hibernate flush/dirty-checking trap (the Krzysztof Slusarski "existsById"
 * story).
 *
 * Setup: persist N entities into a single Session/transaction and keep them all
 * managed (never cleared/evicted) -- this is the persistence context an app
 * accumulates over a long unit-of-work (a big batch job, a long HTTP request that
 * touched a lot of rows, etc).
 *
 * "hibernate-bad" then repeatedly runs a tiny indexed lookup query
 * ({@code select w.id from WidgetEntity w where w.id = :id}) with Hibernate's
 * default FlushMode.AUTO left in place. Before Hibernate can run ANY query it must
 * first flush -- and flushing means dirty-checking every managed entity currently in
 * the persistence context (comparing current field values against the loaded
 * snapshot) so pending changes are visible to the query. With N entities managed,
 * that's an O(N) scan before every single one-row lookup, so total cost is O(N *
 * calls) -- CPU dominated by Hibernate's flush/dirty-check machinery, not by the
 * actual query.
 *
 * "hibernate-fixed" runs the identical lookup query but with
 * {@code session.setHibernateFlushMode(FlushMode.COMMIT)} set once beforehand, so
 * Hibernate skips the pre-query auto-flush entirely (changes are only flushed at
 * commit) -- each lookup is just the cheap indexed SELECT it always should have
 * been.
 */
final class HibernateBug {
    private static final int DURATION_SEC = Integer.getInteger("lab.durationSec", 35);
    private static final int N = Integer.getInteger("lab.hibernateN",
            Integer.parseInt(System.getenv().getOrDefault("HIBERNATE_N", "8000")));

    static void run(boolean fixed, String resultsDir) throws Exception {
        String mode = fixed ? "hibernate-fixed" : "hibernate-bad";
        String csv = resultsDir + "/hibernate_cpu.csv";

        Configuration cfg = new Configuration();
        cfg.setProperty("hibernate.connection.driver_class", "org.h2.Driver");
        cfg.setProperty("hibernate.connection.url",
                "jdbc:h2:mem:hibbench_" + mode.replace('-', '_') + ";DB_CLOSE_DELAY=-1");
        cfg.setProperty("hibernate.connection.username", "sa");
        cfg.setProperty("hibernate.connection.password", "");
        cfg.setProperty("hibernate.hbm2ddl.auto", "create");
        cfg.setProperty("hibernate.show_sql", "false");
        cfg.addAnnotatedClass(WidgetEntity.class);

        StandardServiceRegistry registry =
                new StandardServiceRegistryBuilder().applySettings(cfg.getProperties()).build();
        SessionFactory sf;
        try {
            sf = cfg.buildSessionFactory(registry);
        } catch (Exception e) {
            StandardServiceRegistryBuilder.destroy(registry);
            throw e;
        }

        try {
            Session session = sf.openSession();
            Transaction tx = session.beginTransaction();

            long setupStart = System.currentTimeMillis();
            for (long id = 1; id <= N; id++) {
                session.persist(new WidgetEntity(id, "widget-" + id, (int) (id % 1000),
                        id % 2 == 0, System.currentTimeMillis()));
                if (id % 500 == 0) {
                    session.flush(); // batch-materialize inserts during setup; entities stay managed
                }
            }
            session.flush();
            long setupMs = System.currentTimeMillis() - setupStart;

            // Entities remain managed in this still-open session/transaction. Only now do the
            // two variants diverge.
            if (fixed) {
                session.setHibernateFlushMode(FlushMode.COMMIT);
            }

            CpuSampler sampler = new CpuSampler(csv, mode, 1000);
            sampler.start();

            long endAt = System.currentTimeMillis() + DURATION_SEC * 1000L;
            long checks = 0;
            long found = 0;
            ThreadLocalRandom rnd = ThreadLocalRandom.current();

            while (System.currentTimeMillis() < endAt) {
                long id = 1 + rnd.nextInt(N);
                Long r = session.createQuery(
                                "select w.id from WidgetEntity w where w.id = :id", Long.class)
                        .setParameter("id", id)
                        .uniqueResultOptional()
                        .orElse(null);
                if (r != null) found++;
                checks++;
            }

            sampler.stop();
            tx.commit();
            session.close();

            double elapsedS = DURATION_SEC;
            double checksPerSec = checks / elapsedS;
            Utils.appendThroughputRow(resultsDir, mode, "checks_per_sec", checks, elapsedS, checksPerSec,
                    "N=" + N + " found=" + found + " setup_ms=" + setupMs);
            System.out.printf("[%s] N=%d checks=%d found=%d throughput=%.2f checks/sec setup_ms=%d%n",
                    mode, N, checks, found, checksPerSec, setupMs);
        } finally {
            sf.close();
            StandardServiceRegistryBuilder.destroy(registry);
        }
    }
}
