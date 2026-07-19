package lab;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/** Trivial mapped entity used to fill the Hibernate persistence context. */
@Entity
@Table(name = "widget")
public class WidgetEntity {
    @Id
    private Long id;
    private String name;
    private int amount;
    private boolean flag;
    private long updatedAt;

    protected WidgetEntity() {
        // no-arg ctor required by Hibernate
    }

    public WidgetEntity(Long id, String name, int amount, boolean flag, long updatedAt) {
        this.id = id;
        this.name = name;
        this.amount = amount;
        this.flag = flag;
        this.updatedAt = updatedAt;
    }

    public Long getId() {
        return id;
    }

    public String getName() {
        return name;
    }

    public int getAmount() {
        return amount;
    }

    public boolean isFlag() {
        return flag;
    }

    public long getUpdatedAt() {
        return updatedAt;
    }
}
