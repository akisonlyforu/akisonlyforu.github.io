// Trivial class whose compiled bytes are re-defined once per throwaway
// classloader in the metaspace experiment. Each defineClass() of these bytes
// under a fresh loader creates a DISTINCT runtime klass in Metaspace, even
// though the name "Leak" never changes -- that is what fills Metaspace.
public class Leak {
    // A few members so the klass is not degenerate (methods/fields all live in
    // Metaspace as part of the class metadata).
    public int a, b, c;
    public long sum() { return (long) a + b + c; }
    public String describe() { return "leak-" + a + "-" + b + "-" + c; }
}
